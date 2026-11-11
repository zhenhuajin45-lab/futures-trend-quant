"""Real SDK -> consumer -> coordinator cache -> final quote guard, no account engine."""
from pathlib import Path
from collections import Counter
from datetime import datetime,timezone
import argparse,json,queue,time
from futures_trend_quant.live.paper_tq import atomic,TQ_ADDED,final_source
from futures_trend_quant.live.paper_config import load_paper_config
from futures_trend_quant.live.paper_tq_market import OwnedFeed
from futures_trend_quant.live.paper_data import MarketWorker
from futures_trend_quant.live.paper_five import FiveRunner
from futures_trend_quant.live.paper_quote_cache import latest_quote


def verify(path,seconds=90):
    path=Path(path).resolve();root=path.parents[1];run_id='DATA_CHAIN_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    isolated=Path('data/cache/tq_data_chain')/run_id
    private=json.loads(path.read_text(encoding='utf8'));private['evidence_dir']=str(isolated/'evidence')
    cfg=root/'config'/('tq_'+run_id+'.local.json');atomic(cfg,private);c=load_paper_config(cfg,root=root)
    runner=FiveRunner.__new__(FiveRunner);runner.c=c;runner.engine=None;runner.records={};runner.evidence=Path(c['evidence_dir'])
    runner.accepted_quotes={};runner.quote_feed_counts=Counter();runner.quote_feed_last={}
    atomic(runner.evidence/'run_manifest.json',dict(run_id=run_id,config_sha256=c['config_sha256'],synthetic_host_binding=True,no_account_engine=True))
    feed=OwnedFeed(c,cfg,run_id);feed.start();workers={};rows={}
    try:
        for cs in TQ_ADDED:
            child=dict(c,csymbol=cs,tq_run_id=run_id,tq_spool=str(feed.spool),state_dir=str(root/isolated/'state'/cs.replace('.','_')),
                evidence_dir=str(root/isolated/'products'/cs.replace('.','_')))
            w=MarketWorker(child);w.thread.start();workers[cs]=w
            rows[cs]=dict(quotes=0,new_m1=0,final_guard_passes=0,rejections={},fault=None)
        end=time.monotonic()+seconds+60
        while time.monotonic()<end:
            for cs,w in workers.items():
                while True:
                    try:e=w.events.get_nowait()
                    except queue.Empty:break
                    if e[0]=='METADATA':
                        m=e[1];rows[cs]['symbol']=m['symbol'];runner.records[m['symbol']]=dict(tick=m['spec']['price_tick'])
                    elif e[0]=='INITIAL_CACHE':rows[cs]['coverage']=e[2]
                    elif e[0]=='NEW_BARS':rows[cs]['new_m1']+=len(e[1])
                    elif e[0]=='QUOTE':
                        _,symbol,q,received=e;rows[cs]['quotes']+=1
                        try:
                            runner.accept_quote(symbol,q,received)
                            now=datetime.now(timezone.utc)
                            final_source(root,c,c['config_sha256'],symbol,q,now,opening=True)
                            # Exit-shaped validation skips account/entry service proof;
                            # it does not issue an exit or create a position/order.
                            result=latest_quote(root,c,c['config_sha256'],dict(symbol=symbol,side=1,position_effect=2,price=q['ask'],tick=q['tick']),now)
                            assert result['source_run_id']==run_id
                            rows[cs]['final_guard_passes']+=1
                        except (ValueError,OSError) as exc:
                            key=type(exc).__name__+':'+str(exc)[:90];rows[cs]['rejections'][key]=rows[cs]['rejections'].get(key,0)+1
                    else:rows[cs]['fault']=e
            if any(r['fault'] for r in rows.values()) or not feed.is_alive():break
            if all(r['final_guard_passes']>0 and r['new_m1']>0 for r in rows.values()):break
            time.sleep(.05)
        result=dict(run_id=run_id,completed_at=datetime.now(timezone.utc).isoformat(),products=rows,
            account_engine_created=False,broker_transport_created=False,orders_sent=0,synthetic_host_binding=True,
            natural_order_acceptance='NOT_EVALUATED',passed=all(r['final_guard_passes']>0 and r['new_m1']>0 and not r['fault'] for r in rows.values()))
        atomic(root/isolated/'verification.local.json',result)
    finally:
        for obj in [feed]+[w.thread for w in workers.values()]:
            if obj.is_alive():obj.terminate();obj.join(timeout=2)
        for w in workers.values():w.events.cancel_join_thread();w.events.close()
    print(json.dumps(dict(receipt=str(root/isolated/'verification.local.json'),**result),ensure_ascii=False,default=str))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);a=p.parse_args()
    raise SystemExit(0 if verify(a.config)['passed'] else 1)
