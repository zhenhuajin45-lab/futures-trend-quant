"""Extend every truncated actual leg uniformly; restore causal core for old-leg exits."""
from pathlib import Path
from collections import deque
from concurrent.futures import ProcessPoolExecutor,as_completed
from dataclasses import asdict
from datetime import datetime
import argparse,gzip,json,os,shutil
import pandas as pd
from backtest.forward_v3 import read,write,digest
from backtest.aggregation import SequentialAggregator
from futures_trend_quant.data.gm_export import export_history_to_parquet
from futures_trend_quant.data.gm_bars import bars_from_records
from futures_trend_quant.core.pipeline import StrategyRuntimeBook,CoreStepInput,run_core_step
from futures_trend_quant.core.facts_state import update_facts_state
from futures_trend_quant.core.models import TrendState
from futures_trend_quant.core.entry import AnchorSelection

ROOT=Path(__file__).resolve().parents[2]


def extend_replay(task,original_root,new_root,old_replay,new_replay):
    symbol=task['symbol'];new_root=Path(new_root);original_root=Path(original_root)
    old_path=Path(old_replay)/(symbol+'.jsonl.gz');target=Path(new_replay)/(symbol+'.jsonl.gz')
    frame=pd.read_parquet(new_root/task['path']);old=pd.read_parquet(original_root/task['path'])
    if len(frame)==len(old):shutil.copyfile(old_path,target);return dict(symbol=symbol,added_m15=0,parity_checked=0)
    with gzip.open(old_path,'rt',encoding='utf8') as f:
        header=json.loads(next(f));rows=[json.loads(line) for line in f]
    old_by_time={pd.Timestamp(r['decision_eob']):r for r in rows};last_old=pd.Timestamp(old.eob.max())
    check_times=set(sorted(old_by_time)[-20:]);rules=read(ROOT/'config/strategy.json')['rules']
    runtime=StrategyRuntimeBook();m1=deque(maxlen=1200);m5=deque(maxlen=1200);m15=deque(maxlen=1200)
    a5=SequentialAggregator(5,'300s');a15=SequentialAggregator(15,'900s');n15=0;parity=0;added=[]
    schedule={pd.Timestamp(r['eob']):r['trading_date'] for r in read(new_root/(task['csymbol']+'_schedule.json'))}
    selection=read(new_root/(task['csymbol']+'_selection.json'))
    for i,bar in enumerate(bars_from_records(frame.to_dict('records'))):
        m1.append(bar);b5=a5.update(bar)
        if b5 is not None:m5.append(b5)
        b15=a15.update(bar)
        if b15 is None:continue
        m15.append(b15);n15+=1;now=pd.Timestamp(bar.eob);cached=old_by_time.get(now)
        try:
            if now in check_times or now>last_old:
                result=run_core_step(CoreStepInput(symbol,symbol,tuple(m15),tuple(m5),tuple(m1),rules),runtime)
                if now in check_times:
                    expected=cached['groups']['S0']['trend_decision']
                    assert asdict(result.trend.state)==expected['state'],(symbol,now,'TREND_PREFIX_PARITY')
                    assert result.trend.confirmed==expected['confirmed'];parity+=1
                else:
                    day=schedule.get(pd.Timestamp(b15.eob))
                    if day is None:raise ValueError('extended bar outside source calendar proxy')
                    # Old-leg entry remains disallowed after its recorded selected period.
                    if selection.get(day)==symbol:raise ValueError('tail unexpectedly changes selected entry scope')
                    core=dict(result.facts_row);core.update(high=b15.high,low=b15.low,entry_evaluation=None)
                    added.append(dict(i15=n15-1,m1_index=i,trading_date=day,selected=False,complete_m15=True,
                        decision_eob=bar.eob.isoformat(),bar=asdict(b15),groups={'S0':core}))
            else:update_facts_state(runtime.facts.for_csymbol(symbol),list(m15),rules)
        except ValueError as exc:
            if any(x in str(exc).lower() for x in ('warmup','unavailable','requires')):continue
            raise
        if cached is not None:
            core=cached['groups']['S0'];runtime.trends[symbol]=TrendState(**core['trend_decision']['state'])
            entry=core.get('entry_evaluation')
            if entry and entry.get('anchor'):
                anchor=AnchorSelection(**entry['anchor']);runtime.anchors[symbol+'|'+anchor.direction]=anchor
    assert parity==len(check_times),(symbol,'incomplete prefix parity')
    header.update(input_sha256=digest(new_root/task['path']),v8_original_input_sha256=digest(original_root/task['path']),
        v8_original_replay_sha256=digest(old_path),v8_prefix_parity=parity)
    with gzip.open(target,'wt',encoding='utf8') as f:
        f.write(json.dumps(header)+'\n')
        for row in rows+added:f.write(json.dumps(row,default=str,ensure_ascii=False,allow_nan=False)+'\n')
    return dict(symbol=symbol,added_m15=len(added),parity_checked=parity)


def main():
    p=argparse.ArgumentParser();p.add_argument('--original-root',type=Path,required=True)
    p.add_argument('--old-replay',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--workers',type=int,default=3);p.add_argument('--resume-replay',action='store_true');a=p.parse_args()
    source=a.output/'data';replay=a.output/'replay';source.mkdir(parents=True,exist_ok=True);replay.mkdir(exist_ok=True)
    original=read(a.original_root/'manifest.json');manifest=json.loads(json.dumps(original));audit=[]
    if not a.resume_replay:
        from gm.api import history,set_token,get_instruments
        set_token(os.environ['GM_TOKEN'])
        for cs in original['products']:
            for suffix in ['_schedule.json','_selection.json']:shutil.copyfile(a.original_root/(cs+suffix),source/(cs+suffix))
        for task in manifest['tasks']:
            path=a.original_root/task['path'];old=pd.read_parquet(path);start=pd.Timestamp(old.eob.max());end=pd.Timestamp('2026-11-02T15:00:00+08:00')
            item=dict(symbol=task['symbol'],source_sha256=digest(path),source_last_eob=start.isoformat(),request_end=end.isoformat(),added_m1=0)
            if start<end:
                raw=a.output/(task['symbol']+'_tail.parquet')
                try:
                    export_history_to_parquet(symbol=task['symbol'],start_time=(start-pd.Timedelta(minutes=1)).to_pydatetime(),
                        end_time=end.to_pydatetime(),output_path=raw,history_fetcher=history,chunk_days=20)
                    tail=pd.read_parquet(raw);tail['eob']=pd.to_datetime(tail.eob,utc=True).dt.tz_convert('Asia/Shanghai')
                    common=old.merge(tail,on='eob',suffixes=('_old','_new'))
                    assert len(common)>0
                    for field in ['open','high','low','close','volume','position']:
                        assert (common[field+'_old']==common[field+'_new']).all(),(task['symbol'],field,'REVISED_OVERLAP')
                    tail=tail[tail.eob>start];old=pd.concat([old,tail],ignore_index=True).sort_values('eob')
                    item.update(added_m1=len(tail),provider_response_sha256=digest(raw))
                except Exception as exc:
                    item.update(status='TAIL_UNAVAILABLE',error_type=type(exc).__name__)
                    if 'REVISED_OVERLAP' in str(exc):raise
            target=source/task['path'];target.parent.mkdir(parents=True,exist_ok=True);old.to_parquet(target,index=False)
            task['rows']=len(old);item['new_sha256']=digest(target);audit.append(item)
            print('TAIL_FETCH',task['symbol'],item['added_m1'],item.get('status','OK'),flush=True)
        write(source/'manifest.json',manifest);write(a.output/'fetch_audit.json',audit)
    else:manifest=read(source/'manifest.json')
    results=[]
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        jobs=[pool.submit(extend_replay,t,str(a.original_root),str(source),str(a.old_replay),str(replay)) for t in manifest['tasks']
            if not (replay/(t['symbol']+'.jsonl.gz')).exists()]
        for job in as_completed(jobs):
            result=job.result();results.append(result);write(a.output/'replay_audit.json',results)
            print('TAIL_REPLAY',result,flush=True)
    print('V8_EXTENDED_TAIL_INPUTS_READY',flush=True)


if __name__=='__main__':main()
