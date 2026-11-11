"""Stopped-data, same-input consumer/calendar comparison; no trading SDK calls."""
from pathlib import Path
from datetime import datetime
import argparse,hashlib,json,shutil,time
import numpy as np
from futures_trend_quant.live.paper_calendar import PaperCalendar
from tools.paper_notice_details import LocalBars,M5DivergenceMonitor


def audit(client,output):
    rt=client/'_strategy_runtime';ev=rt/'evidence/paper_observation_five';review=ev/'reviews/20261111_day_postclose'
    read=lambda p:json.loads(p.read_text(encoding='utf-8-sig'))
    manifest=Path(read(review/'postclose_freeze_pointer.local.json')['manifest']);frozen=read(manifest)
    files={Path(row['source']):manifest.parent/row['copy'] for row in frozen['files']}
    assert all(hashlib.sha256(files[Path(row['source'])].read_bytes()).hexdigest()==row['sha256'] for row in frozen['files'])
    old={}
    for key,relative in [('calendar','futures_trend_quant/live/paper_calendar.py'),('consumer','tools/paper_notice_details.py')]:
        namespace={'__name__':'r9_frozen_'+key};exec(compile(files[rt/relative].read_text(encoding='utf-8-sig'),relative,'exec'),namespace);old[key]=namespace
    status=read(ev/'status.json');now=datetime.fromisoformat(status['at']);before={};after={};calendars={}
    loaders=[old['consumer']['LocalBars'](rt),LocalBars(rt)]
    for cs,p in status['products'].items():
        for loader,result in zip(loaders,(before,after)):
            began=time.perf_counter()
            try:
                bars=loader.load(cs,p['symbol'],now)
                result[cs]=dict(success=True,bars=len(bars),last_eob=bars[-1].eob.isoformat())
            except (ValueError,KeyError,TypeError,OSError) as exc:result[cs]=dict(success=False,error=type(exc).__name__,detail=str(exc))
            result[cs]['seconds']=time.perf_counter()-began
        meta=read(ev/'products'/cs.replace('.','_')/'market_metadata.local.json');made=[];elapsed=[]
        for cls in (old['calendar']['PaperCalendar'],PaperCalendar):
            began=time.perf_counter();made.append(cls(meta['dates'],meta['sessions'],cs));elapsed.append(time.perf_counter()-began)
        a,b=made
        assert np.array_equal(a.ns,b.ns) and np.array_equal(a.days,b.days) and a.sections==b.sections
        assert a.daily_counts==b.daily_counts and a.first_open_ns==b.first_open_ns and a.close_ns==b.close_ns
        x,y=a.policy.products[cs],b.policy.products[cs]
        assert set(x)==set(y)
        for key in x:
            assert np.array_equal(x[key],y[key]) if isinstance(x[key],np.ndarray) else x[key]==y[key],key
        calendars[cs]=dict(equivalent=True,minutes=len(b.ns),old_seconds=elapsed[0],new_seconds=elapsed[1],
            exact_clock_days_sections_deadlines_blackouts=True,clock_sha256=hashlib.sha256(b.ns.tobytes()).hexdigest())
        print(json.dumps(dict(csymbol=cs,old_consumer=before[cs]['success'],new_consumer=after[cs]['success'],calendar_equivalent=True)),flush=True)
    if not all(r['success'] for r in after.values()):raise ValueError('R9_ACTUAL_CONSUMER_FAILED')
    # The real monitor may write only to an isolated clone; actual raw inputs,
    # execution state and live notification outbox remain untouched.
    view=output/'monitor_client';clone=view/'_strategy_runtime'
    shutil.copytree(rt/'state/paper_observation_five',clone/'state/paper_observation_five')
    for rel in ('config/paper_observation_five.local.json','config/paper_feishu.local.json','config/strategy.json',
        'evidence/paper_observation_five/status.json','evidence/paper_observation_five/run_manifest.json'):
        p=clone/rel;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(rt/rel,p)
    for name in ('m5_divergence_cursor.local.json',):
        if (ev/name).exists():shutil.copy2(ev/name,clone/'evidence/paper_observation_five'/name)
    for cs in status['products']:
        key=cs.replace('.','_');folder=clone/'state/paper_observation_five/products'/key;cp=read(folder/'core_checkpoint.json')
        seed=Path(cp['seed_cache']['path']);rel=seed.relative_to(rt);dest=clone/rel
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(seed,dest)
        shutil.copy2(seed.parent/'input_manifest.json',dest.parent/'input_manifest.json');cp['seed_cache']['path']=str(dest)
        if cp.get('tq_input_prefix'):cp['tq_input_prefix']['path']=str(folder/'tq_core_m1.jsonl')
        (folder/'core_checkpoint.json').write_text(json.dumps(cp),encoding='utf8')
        dest=clone/'evidence/paper_observation_five/products'/key;dest.mkdir(parents=True,exist_ok=True)
        for name in ('m1_observed.jsonl','market_metadata.local.json'):
            if (ev/'products'/key/name).exists():shutil.copy2(ev/'products'/key/name,dest/name)
    protected=hashlib.sha256((clone/'state/paper_observation_five/execution.json').read_bytes()).hexdigest()
    monitor=M5DivergenceMonitor(view);began=time.perf_counter();sent=monitor.poll(now);first_seconds=time.perf_counter()-began
    assert monitor.poll(now)==[] and protected==hashlib.sha256((clone/'state/paper_observation_five/execution.json').read_bytes()).hexdigest()
    cursor=read(clone/'evidence/paper_observation_five/m5_divergence_cursor.local.json')
    assert not {k:v for k,v in cursor.get('errors',{}).items() if k in status['products']}
    replay=read(review/'causal_core_replay.local.json');assert replay['evaluations']==60 and replay['mismatches']==0
    result=dict(scope='FROZEN_SOURCE_VS_R9_SAME_STOPPED_INPUTS;ISOLATED_REAL_M5_MONITOR_NO_NETWORK',
        frozen_files=len(frozen['files']),frozen_sha_verified=True,old_consumers=before,new_consumers=after,
        calendars=calendars,monitor=dict(first_seconds=first_seconds,local_only_cards=len(sent),duplicate_cards=0,execution_state_unchanged=True),
        core_comparison=dict(evaluations=60,mismatches=0,core_unchanged=True,natural_orders_verified=False),
        source_known_at='HISTORICAL_MISSING_CLOCKS_REMAIN_UNKNOWN')
    output.mkdir(exist_ok=True,parents=True);(output/'historical_audit.local.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf8');return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--client',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();audit(a.client.resolve(),a.output.resolve())
