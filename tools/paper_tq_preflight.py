"""Isolated real data + core preflight. Never constructs an account engine/transport."""
from pathlib import Path
import argparse
import json
import queue
import time
import shutil
from datetime import datetime,timezone
from futures_trend_quant.live.paper_config import load_paper_config
from futures_trend_quant.live.paper_tq import TQ_ADDED,atomic,sha
from futures_trend_quant.live.paper_tq_market import OwnedFeed
from futures_trend_quant.live.paper_data import MarketWorker,SignalWorker


def verify(config_path,timeout=1800,resume_state=None,resume_profile=None):
    config_path=Path(config_path).resolve();root=config_path.parents[1];c=load_paper_config(config_path,root=root)
    run_id='PREFLIGHT_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    isolated=root/'data/cache/tq_preflight'/run_id
    production_digest=c['config_sha256']
    private=json.loads(config_path.read_text(encoding='utf8'))
    private['evidence_dir']=str(Path('data/cache/tq_preflight')/run_id/'feed_evidence')
    config_path=root/'config'/('tq_preflight_'+run_id+'.local.json');atomic(config_path,private)
    c=load_paper_config(config_path,root=root)
    if resume_state is not None:
        resume_state=Path(resume_state).resolve()
        if not resume_state.is_relative_to((root/'data/cache/tq_preflight').resolve()):raise ValueError('TQ_RESUME_STATE_BOUNDARY')
        prior=json.loads((resume_state/'preflight.local.json').read_text(encoding='utf8'))
        if prior.get('production_config_sha256')!=production_digest:
            if resume_profile is None:raise ValueError('TQ_RESUME_PROFILE_CHANGED')
            resume_profile=Path(resume_profile).resolve()
            if not resume_profile.is_relative_to(root/'config') or sha(resume_profile)!=prior.get('production_config_sha256'):raise ValueError('TQ_RESUME_ORIGINAL_PROFILE_UNPROVED')
            before=json.loads(resume_profile.read_text(encoding='utf8'));after=json.loads((root/'config/paper_observation_five.local.json').read_text(encoding='utf8'))
            before['tq_feed']['python']=after['tq_feed']['python']
            if before!=after:raise ValueError('TQ_RESUME_ONLY_PINNED_INTERPRETER_MIGRATION_ALLOWED')
        from futures_trend_quant.live.paper_tq import load_manifest
        manifest=load_manifest(c,root)
        for cs in TQ_ADDED:
            key=cs.replace('.','_');cp=json.loads((resume_state/'state'/key/'core_checkpoint.json').read_text())
            cache=Path(cp['seed_cache']['path']).resolve()
            if cp['runtime'].get('core_contract')!='CLOSED_FACTS_V2' or cp['seed_cache']['symbol']!=manifest['products'][cs]['symbol'] or not cache.is_relative_to((root/'data/cache/paper_market').resolve()) or sha(cache)!=cp['seed_cache']['sha256']:
                raise ValueError('TQ_RESUME_PREFIX_CHANGED')
            target=isolated/'state'/key;shutil.copytree(resume_state/'state'/key,target)
            atomic(target/'contract_pin.json',dict(symbol=manifest['products'][cs]['symbol'],config_sha256=c['config_sha256']))
    result=dict(run_id=run_id,orders_enabled=False,account_engine_created=False,broker_transport_created=False,
        started_at=datetime.now(timezone.utc).isoformat(),products={},config_sha256=c['config_sha256'],production_config_sha256=production_digest,
        resumed_from=None if resume_state is None else str(resume_state),
        resume_profile_sha256=None if resume_profile is None else sha(resume_profile))
    feed=OwnedFeed(c,config_path,run_id);feed.start();workers={};cores={}
    try:
        for cs in TQ_ADDED:
            child=dict(c,csymbol=cs,tq_run_id=run_id,tq_spool=str(feed.spool),
                state_dir=str(isolated/'state'/cs.replace('.','_')),evidence_dir=str(isolated/'evidence'/cs.replace('.','_')))
            market=MarketWorker(child);core=SignalWorker(child)
            market.thread.start();core.thread.start();workers[cs]=market;cores[cs]=core
            result['products'][cs]=dict(quote_count=0,new_bar_count=0,core_ready=False,coverage=None,fault=None)
        end=time.monotonic()+timeout;ready_at=None
        while time.monotonic()<end:
            for cs,m in workers.items():
                row=result['products'][cs]
                while True:
                    try:event=m.events.get_nowait()
                    except queue.Empty:break
                    kind=event[0]
                    if kind=='METADATA':cores[cs].inbox.put(('CALENDAR',event[1]));row['symbol']=event[1]['symbol']
                    elif kind=='INITIAL_CACHE':
                        row['coverage']=event[2];cores[cs].inbox.put(('INITIAL_CACHE',event[1]));row['cache']=event[1]
                    elif kind=='QUOTE':row['quote_count']+=1;row['latest_quote']=dict(quote=event[2],received_at=event[3])
                    elif kind=='NEW_BARS':row['new_bar_count']+=len(event[1]);cores[cs].inbox.put(('NEW',event[1],event[2]))
                    else:row['fault']=event
                while True:
                    try:event=cores[cs].outbox.get_nowait()
                    except queue.Empty:break
                    if event[0]=='CORE_READY':row.update(core_ready=True,core=event[1])
                    elif event[0] in ('CORE_FAILED','WARMUP_FAILED'):row['fault']=event
                    elif event[0]=='CORE_PROGRESS':row['core_progress']=event[1]
            atomic(isolated/'preflight.local.json',result)
            if not feed.is_alive() or any(r['fault'] for r in result['products'].values()):break
            ready=all(r['core_ready'] and r['quote_count']>0 and (r['coverage'] or {}).get('qualified') for r in result['products'].values())
            if ready and ready_at is None:ready_at=time.monotonic()
            if ready and all(r['new_bar_count']>0 for r in result['products'].values()):break
            if ready_at is not None and time.monotonic()-ready_at>=80:break
            time.sleep(.1)
        result.update(completed_at=datetime.now(timezone.utc).isoformat(),feed_alive_at_completion=feed.is_alive(),
            passed=all(r['core_ready'] and r['quote_count']>0 and not r['fault'] and (r['coverage'] or {}).get('qualified') for r in result['products'].values()))
        from futures_trend_quant.live.paper_tq import latest_committed
        result['feed_proof']=latest_committed(feed.spool/'health.jsonl')
        result['natural_order_acceptance']='NOT_EVALUATED_NO_BROKER'
        atomic(isolated/'preflight.local.json',result)
    finally:
        # Only processes constructed by this isolated data/core check; never a platform strategy.
        for obj in [feed]+[w.thread for w in workers.values()]+[w.thread for w in cores.values()]:
            if obj.is_alive():obj.terminate();obj.join(timeout=2)
        for w in cores.values():
            for q in (w.inbox,w.outbox):
                q.cancel_join_thread();q.close()
        for w in workers.values():w.events.cancel_join_thread();w.events.close()
    print(json.dumps(dict(receipt=str(isolated/'preflight.local.json'),passed=result.get('passed'),products=result['products']),ensure_ascii=False,default=str))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--timeout',type=int,default=1800)
    p.add_argument('--resume-state',type=Path)
    p.add_argument('--resume-profile',type=Path)
    a=p.parse_args();r=verify(a.config,a.timeout,a.resume_state,a.resume_profile);raise SystemExit(0 if r['passed'] else 1)
