"""Replay actual frozen inputs through SignalWorker, with isolated persistence."""
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
import argparse,hashlib,json,threading,queue
import pandas as pd
from futures_trend_quant.live.paper_config import load_paper_config
from futures_trend_quant.live.paper_data import SignalWorker
from futures_trend_quant.live.paper_closed_m1 import POLICY
from futures_trend_quant.data.gm_bars import bar_from_mapping


def replay(client,output):
    rt=client/'_strategy_runtime';ev=rt/'evidence/paper_observation_five';review=ev/'reviews/20261111_day_postclose'
    read=lambda p:json.loads(p.read_text(encoding='utf-8-sig'));pre=review/'pre_afternoon_130929';pm=read(pre/'freeze_manifest.local.json')
    pi={rt/row['file']:pre/row['copy'] for row in pm['files']}
    post=Path(read(review/'postclose_freeze_pointer.local.json')['manifest']);fi={Path(row['source']):post.parent/row['copy'] for row in read(post)['files']}
    obj=lambda p:read(pi[p] if p in pi else fi[p])
    c=load_paper_config(rt/'config/paper_observation_five.local.json',root=rt);c['gm_closed_m1_policy']=POLICY
    rows=[json.loads(line) for line in fi[ev/'signals.jsonl'].read_bytes().splitlines()]
    rows=[r for r in rows if pd.Timestamp(r['signal_ready_at'])>=pd.Timestamp('2026-11-11T12:45:36+08:00')]
    checks=[];products={}
    def wait(worker):
        results=[]
        while True:
            event=worker.outbox.get(timeout=90)
            if event[0] in ('CORE_FAILED','WARMUP_FAILED'):raise RuntimeError(str(event))
            if event[0]=='CORE_RESULT':results.append(event[1])
            if event[0]=='CORE_READY':return results
    for cs in c['universe']:
        slug=cs.replace('.','_');folder=output/'worker_replay'/slug;folder.mkdir(parents=True,exist_ok=False)
        path=rt/'state/paper_observation_five/products'/slug/'core_checkpoint.json';cp=obj(path)
        cp_path=folder/'core_checkpoint.json';cp_path.write_text(json.dumps(cp),encoding='utf8')
        for name in ('tq_core_m1.jsonl','gm_core_m1.jsonl'):
            original=path.parent/name
            if original in pi:
                (folder/name).write_bytes(pi[original].read_bytes())
                key='tq_input_prefix' if name.startswith('tq') else 'gm_input_prefix'
                if cp.get(key):cp[key]['path']=str(folder/name)
        cp_path.write_text(json.dumps(cp),encoding='utf8')
        current=next(rt/p for p in pm['actual_initial_caches'] if Path(p).parent.name.startswith(slug+'_'))
        m=obj(current);cache=current.parent/'initial_m1.parquet'
        assert hashlib.sha256(cache.read_bytes()).hexdigest()==m['sha256']
        raw=dict(path=str(cache),sha256=m['sha256'],symbol=m['symbol'],cutoff=m.get('known_at',m.get('requested_at')))
        config=dict(c,csymbol=cs,state_dir=str(folder),evidence_dir=str(folder/'evidence'))
        worker=SignalWorker(config,child=True);thread=threading.Thread(target=worker._run,daemon=True);thread.start()
        meta=obj(ev/'products'/slug/'market_metadata.local.json');worker.inbox.put(('CALENDAR',meta));worker.inbox.put(('INITIAL_CACHE',raw))
        assert wait(worker)==[]
        inputs=[];external=cs in ('CZCE.MA','DCE.EG','DCE.JD','CZCE.CJ','SHFE.AG')
        journal=rt/'state/paper_observation_five/products'/slug/'tq_core_m1.jsonl' if external else ev/'products'/slug/'m1_observed.jsonl'
        for line in fi[journal].read_bytes().splitlines():
            value=json.loads(line);bar=bar_from_mapping(value['bar'] if external else dict(value,frequency='60s'))
            known=value['recorded_at'] if external else value['local_observed_at']
            if bar.symbol==raw['symbol']:inputs.append((bar,pd.Timestamp(known)))
        count=0
        for row in sorted([r for r in rows if r['csymbol']==cs],key=lambda r:r['bar_eob']):
            bars={b.identity:b for b,known in inputs if known<=pd.Timestamp(row['signal_ready_at']) and b.eob<=pd.Timestamp(row['bar_eob'])}
            known_ids={b.identity for b in worker.bars};new=[b for b in sorted(bars.values(),key=lambda b:b.eob) if b.identity not in known_ids]
            worker.inbox.put(('NEW',new,dict(source='FROZEN_SAME_SOURCE_REPLAY',returned_at=row['signal_ready_at'],actual_original_clocks_preserved_in_source=True)))
            results=wait(worker);assert len(results)==1,(cs,row['bar_eob'],len(results))
            result=results[0];diff=[]
            for key in ('close','ema34','atr14','slope_state','h1_absolute','h1_source','trade_permission','entry_permission','top_div','bottom_div','top_divergence_event','bottom_divergence_event','trend_inputs','trend_decision'):
                if json.dumps(result.facts_row.get(key),sort_keys=True,default=str)!=json.dumps(row['facts'].get(key),sort_keys=True,default=str):diff.append(key)
            def entry(x):return None if not x else {k:x.get(k) for k in ('accepted','path','direction','reason','evidence_id')}
            if entry(None if result.entry is None else asdict(result.entry))!=entry(row.get('entry')):diff.append('entry')
            def audits(items):return [{k:x.get(k) for k in ('path','evidence_id','eligible','reasons')} for x in items or []]
            if audits(result.candidate_audit)!=audits(row['facts'].get('candidate_audit')):diff.append('candidate_audit')
            checks.append(dict(csymbol=cs,bar_eob=row['bar_eob'],differences=diff));count+=1
        worker.stop=True;worker.inbox.put(('POSITIONS',{}));thread.join(3);assert not thread.is_alive()
        restarted=SignalWorker(config,child=True)
        before_state=worker.runtime.to_dict();after_state=restarted.runtime.to_dict()
        canonical=lambda value:json.dumps(value,sort_keys=True,default=str)
        if canonical(before_state)!=canonical(after_state):
            (folder/'restart_difference.local.json').write_text(json.dumps(dict(before=before_state,after=after_state),default=str),encoding='utf8')
            raise ValueError('WORKER_RESTART_STATE_DIFFERENCE:'+','.join(k for k in before_state if canonical(before_state[k])!=canonical(after_state[k])))
        assert restarted.last==worker.last
        products[cs]=dict(evaluations=count,restart_state_equal=True,last_eob=str(worker.last))
        print(json.dumps(dict(csymbol=cs,evaluations=count,mismatches=sum(bool(r['differences']) for r in checks if r['csymbol']==cs))),flush=True)
    report=dict(scope='REAL_SIGNAL_WORKER_LOOP_CALENDAR_INITIAL_CACHE_NEW_CHECKPOINT_RESTART;ISOLATED_NO_ORDERS',
        products=products,evaluations=len(checks),mismatches=sum(bool(r['differences']) for r in checks),checks=checks,
        evaluation_wallclocks='CURRENT_OFFLINE_REPLAY;NOT_CLAIMED_HISTORICAL_KNOWN_AT',core_changed=False,orders_sent=0)
    assert report['evaluations']==60 and not report['mismatches']
    (output/'worker_replay.local.json').write_text(json.dumps(report),encoding='utf8');return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--client',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();replay(a.client.resolve(),a.output.resolve())
