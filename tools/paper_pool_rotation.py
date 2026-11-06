"""Authorized five-product rotation. Prewarm has no account or order worker."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,json,os,queue,shutil,subprocess,sys,time
from tools import paper_core_v2_release as prior
from futures_trend_quant.live.paper_products import FIVE_POOL,OILS_POOL,OILS_POOL_VERSION,PRODUCTS
from futures_trend_quant.live.paper_config import load_paper_config,frozen_core_digest
from futures_trend_quant.live.paper_broker import atomic

ADDED=tuple(cs for cs in OILS_POOL if cs not in FIVE_POOL)
RETAINED=tuple(cs for cs in OILS_POOL if cs in FIVE_POOL)
REMOVED=tuple(cs for cs in FIVE_POOL if cs not in OILS_POOL)


def prepare(client,bundle):
    prior.prepare(client,bundle)
    path=bundle/'client/_strategy_runtime/config/paper_observation_five.local.json'
    old=prior.read(client/'_strategy_runtime/config/paper_observation_five.local.json')
    if old['universe']!=list(FIVE_POOL):raise ValueError('rotation requires the registered original pool')
    c=prior.read(path);c.update(universe=list(OILS_POOL),pool_policy_version=OILS_POOL_VERSION);atomic(path,c)
    check=subprocess.run([sys.executable,'-X','utf8',str(bundle/'client/main.py'),'--self-check','--check-config'],capture_output=True,text=True,encoding='utf8',check=True)
    plan=prior.read(bundle/'release_plan.local.json');plan.update(new_config_sha256=prior.sha(path),
        pool_policy_version=OILS_POOL_VERSION,retained=list(RETAINED),added=list(ADDED),removed=list(REMOVED),
        self_check=json.loads(check.stdout.strip().splitlines()[-1]),scope='USER_AUTHORIZED_POOL_ROTATION_NO_RULE_CHANGE')
    atomic(bundle/'release_plan.local.json',plan)
    print(json.dumps(dict(prepared=True,universe=list(OILS_POOL),core_unchanged=True,orders_sent=0)))


def prewarm(client,bundle):
    """Official metadata and raw recent M1, isolated core state; no synthetic bars."""
    import pandas as pd
    from gm.api import set_token,history_n,fut_get_continuous_contracts,get_instrumentinfos,get_trading_session,get_trading_dates,ADJUST_NONE
    from futures_trend_quant.live.paper_data import MarketWorker,SignalWorker,FIELDS
    from futures_trend_quant.live.paper_runner import warmup_coverage
    from futures_trend_quant.live.paper_calendar import PaperCalendar
    rt=client/'_strategy_runtime';stage=bundle/'client/_strategy_runtime'
    c=load_paper_config(stage/'config/paper_observation_five.local.json',root=stage)
    set_token(prior.read(rt/'config/paper_platform_auth.local.json')['GM_TOKEN'])
    base=rt/'data/cache/paper_pool_rotation'/bundle.name
    if base.exists():raise ValueError('prewarm batch already exists; preserve its evidence')
    base.mkdir(parents=True);results={};workers=[]
    for cs in ADDED:
        key=cs.replace('.','_');state=base/'state/products'/key;ev=base/'evidence/products'/key
        state.mkdir(parents=True);ev.mkdir(parents=True)
        now=pd.Timestamp.now(tz='Asia/Shanghai');dates=get_trading_dates(exchange=PRODUCTS[cs]['exchange'],start_date='2026-05-08',end_date='2026-12-31')
        cutoff=now.date().isoformat() if now.hour>=20 else (now-pd.Timedelta(days=1)).date().isoformat()
        future=sorted(str(d)[:10] for d in dates if str(d)[:10]>cutoff)
        if not future:raise ValueError('no official mapping trading day: '+cs)
        mapping=fut_get_continuous_contracts(csymbol=cs,start_date=future[0],end_date=future[0])
        if not mapping:raise ValueError('no actual mapping: '+cs)
        symbol=mapping[-1]['symbol']
        from futures_trend_quant.live.paper_products import validate_actual
        validate_actual(cs,symbol)
        sessions=get_trading_session(symbols=symbol,df=False)[0]['time_trading']
        spec=get_instrumentinfos(symbols=symbol,fields='symbol,price_tick,multiplier,margin_ratio',df=False)[0]
        calendar=PaperCalendar(dates,sessions,cs)
        metadata=dict(csymbol=cs,symbol=symbol,spec=spec,mapping=mapping,dates=dates,sessions=sessions,source='GM')
        atomic(ev/'market_metadata.local.json',metadata)
        frame=history_n(symbol=symbol,frequency='60s',count=c['seed_m1_count'],end_time=now.to_pydatetime(),fields=FIELDS,skip_suspended=False,adjust=ADJUST_NONE,df=True)
        child=dict(c,root=str(rt),csymbol=cs,state_dir=str(state),evidence_dir=str(ev))
        loader=MarketWorker(child,child=True);loader.symbol=symbol;bars=loader.bars(frame,now)
        # Keep the existing timestamp naming contract used by read-only notifiers.
        cache=rt/'data/cache/paper_market'/(key+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'));cache.mkdir(parents=True)
        frame.to_parquet(cache/'initial_m1.parquet',index=False)
        digest=prior.sha(cache/'initial_m1.parquet')
        atomic(cache/'input_manifest.json',dict(symbol=symbol,rows=len(frame),sha256=digest,gm_original_sha256=digest,zero_trade_normalization=[],purpose='PAPER_WARMUP_STATE_ONLY',requested_at=str(now)))
        coverage=warmup_coverage(bars,calendar,c['warmup_m15'])
        atomic(state/'contract_pin.json',dict(symbol=symbol,config_sha256=c['config_sha256']))
        results[cs]=dict(symbol=symbol,coverage=coverage,rows=len(frame),sha256=digest,seed_path=str(cache/'initial_m1.parquet'),state_dir=str(state),evidence_dir=str(ev),core_ready=False)
        print(json.dumps(dict(csymbol=cs,symbol=symbol,m1=len(frame),coverage=coverage),ensure_ascii=False),flush=True)
        if not coverage['qualified']:
            atomic(bundle/'prewarm.local.json',dict(config_sha256=c['config_sha256'],products=results,qualified=False,orders_sent=0))
            raise ValueError('raw minute coverage incomplete: '+cs)
        worker=SignalWorker(child);worker.thread.start();workers.append((cs,worker))
        worker.inbox.put(('INITIAL_CACHE',dict(path=str(cache/'initial_m1.parquet'),sha256=digest,symbol=symbol,cutoff=str(now))))
    deadline=time.monotonic()+900
    try:
        pending=set(ADDED)
        while pending and time.monotonic()<deadline:
            for cs,w in workers:
                try:
                    while True:
                        event=w.outbox.get_nowait()
                        if event[0] in ('CORE_FAILED','WARMUP_FAILED'):raise ValueError('prewarm core failed: '+cs+' '+str(event[1]))
                        if event[0]=='CORE_READY':
                            results[cs].update(core_ready=True,core=event[1],checkpoint_sha256=prior.sha(Path(results[cs]['state_dir'])/'core_checkpoint.json'))
                            pending.discard(cs);print(json.dumps(dict(csymbol=cs,core_ready=True,last_eob=event[1]['last_eob'])),flush=True)
                except queue.Empty:pass
                if cs in pending and not w.thread.is_alive():raise ValueError('prewarm child exited: '+cs)
            time.sleep(.2)
        if pending:raise TimeoutError('prewarm core deadline')
        atomic(bundle/'prewarm.local.json',dict(at=datetime.now(timezone.utc).isoformat(),config_sha256=c['config_sha256'],products=results,qualified=True,orders_sent=0,broker_worker_started=False,seed_is_seen=True))
    finally:
        # These are exclusively the data-only children created above, never the platform controller.
        for cs,w in workers:
            if w.thread.is_alive():w.thread.terminate();w.thread.join(timeout=5)


def install(client,bundle):
    rt,ev,state,status=prior.flat_stopped(client);plan=prior.read(bundle/'release_plan.local.json')
    stage=bundle/'client';profile=Path('config/paper_observation_five.local.json')
    if Path(plan['active_client']).resolve()!=client.resolve():raise ValueError('wrong client')
    if prior.lfsha(client/'main.py')!=plan['old_main_sha256_lf'] or prior.sha(rt/profile)!=plan['old_config_sha256']:raise ValueError('active baseline changed')
    if prior.lfsha(stage/'main.py')!=plan['new_main_sha256_lf'] or prior.sha(stage/'_strategy_runtime'/profile)!=plan['new_config_sha256']:raise ValueError('release changed')
    old=prior.read(rt/profile);new=prior.read(stage/'_strategy_runtime'/profile)
    if old['universe']!=list(FIVE_POOL) or new['universe']!=list(OILS_POOL) or new.get('pool_policy_version')!=OILS_POOL_VERSION:raise ValueError('pool identity mismatch')
    if {k:v for k,v in old.items() if k not in ('universe','pool_policy_version')}!={k:v for k,v in new.items() if k not in ('universe','pool_policy_version')}:raise ValueError('rotation cannot change risk or strategy parameters')
    if frozen_core_digest(rt)!=plan['new_core_sha256_lf']:raise ValueError('core changed')
    gate=prior.read(bundle/'validation_gate.local.json')
    if not all(gate.get(k) is True for k in ('targeted_tests_passed','native_quote_chain_passed','private_ledger_reconciled','core_unchanged')):raise ValueError('release gates incomplete')
    if gate.get('new_main_sha256_lf')!=plan['new_main_sha256_lf'] or gate.get('new_config_sha256')!=plan['new_config_sha256']:raise ValueError('gate fingerprint mismatch')
    warm=prior.read(bundle/'prewarm.local.json')
    if not warm.get('qualified') or warm['config_sha256']!=plan['new_config_sha256'] or set(warm['products'])!=set(ADDED):raise ValueError('prewarm identity mismatch')
    imports={}
    for cs,p in warm['products'].items():
        if not p.get('core_ready') or not p['coverage']['qualified'] or p['coverage']['expected_m15']!=1200 or p['coverage']['missing_m1']!=0:raise ValueError('prewarm incomplete')
        seed=Path(p['seed_path']).resolve();source=Path(p['state_dir']).resolve();evidence=Path(p['evidence_dir']).resolve()
        if not seed.is_relative_to((rt/'data/cache/paper_market').resolve()) or prior.sha(seed)!=p['sha256']:raise ValueError('prewarm seed changed')
        if not source.is_relative_to((rt/'data/cache/paper_pool_rotation').resolve()) or not evidence.is_relative_to((rt/'data/cache/paper_pool_rotation').resolve()):raise ValueError('prewarm outside batch')
        cp=prior.read(source/'core_checkpoint.json');pin=prior.read(source/'contract_pin.json');meta=prior.read(evidence/'market_metadata.local.json')
        if prior.sha(source/'core_checkpoint.json')!=p['checkpoint_sha256'] or cp['runtime'].get('core_contract')!='CLOSED_FACTS_V2' or cp['last_eob']!=p['core']['last_eob'] or cp['seed_cache']['sha256']!=p['sha256']:raise ValueError('checkpoint proof changed')
        if pin['config_sha256']!=plan['new_config_sha256'] or pin['symbol']!=p['symbol'] or meta['symbol']!=p['symbol'] or meta['csymbol']!=cs:raise ValueError('contract proof mismatch')
        dest=rt/'state/paper_observation_five/products'/cs.replace('.','_')
        if dest.exists():raise ValueError('new product state already exists')
        imports[cs]=(source,evidence)
    retained={cs:prior.sha(rt/'state/paper_observation_five/products'/cs.replace('.','_')/'core_checkpoint.json') for cs in RETAINED}
    backup=bundle/'rollback';backup.mkdir();shutil.copyfile(client/'main.py',backup/'main.py');shutil.copyfile(rt/profile,backup/'paper_observation_five.local.json');shutil.copytree(rt/'state/paper_observation_five',backup/'state')
    shutil.copyfile(stage/'main.py',client/'main.py');shutil.copyfile(stage/'_strategy_runtime'/profile,rt/profile)
    for cs in REMOVED:
        source=(rt/'state/paper_observation_five/products'/cs.replace('.','_')).resolve();dest=(rt/'state/paper_observation_five/retired_products'/bundle.name/cs.replace('.','_')).resolve()
        if not source.is_relative_to((rt/'state/paper_observation_five/products').resolve()) or not dest.is_relative_to((rt/'state/paper_observation_five/retired_products').resolve()):raise ValueError('archive boundary')
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(source),str(dest))
    for cs,(source,evidence) in imports.items():
        dest=rt/'state/paper_observation_five/products'/cs.replace('.','_');shutil.copytree(source,dest)
        edest=ev/'products'/cs.replace('.','_');edest.mkdir(parents=True,exist_ok=True);shutil.copyfile(evidence/'market_metadata.local.json',edest/'market_metadata.local.json')
    for pin in (rt/'state/paper_observation_five/products').glob('*/contract_pin.json'):
        data=prior.read(pin);data['config_sha256']=plan['new_config_sha256'];atomic(pin,data)
    state.update(config_sha256=plan['new_config_sha256'],pause_new=True);atomic(rt/'state/paper_observation_five/execution.json',state)
    atomic(rt/'state/paper_observation_five/migration.local.json',dict(kind=OILS_POOL_VERSION,at=datetime.now(timezone.utc).isoformat(),old_config_sha256=plan['old_config_sha256'],new_config_sha256=plan['new_config_sha256'],broker_checked=True,positions=0,active_intents=0,risk_state_preserved=True,retained_checkpoints=retained,removed=list(REMOVED),added=list(ADDED)))
    assert all(prior.sha(rt/'state/paper_observation_five/products'/cs.replace('.','_')/'core_checkpoint.json')==value for cs,value in retained.items())
    subprocess.run([sys.executable,'-X','utf8',str(client/'main.py'),'--self-check','--check-config'],check=True)
    atomic(bundle/'installed.local.json',dict(at=datetime.now(timezone.utc).isoformat(),core_unchanged=True,risk_state_preserved=True,retained_checkpoints=retained,archived=list(REMOVED),imported=list(ADDED),pause_new=True,orders_sent=0))
    print(json.dumps(dict(installed=True,pause_new=True,retained=2,archived=3,prewarmed_imported=3,orders_sent=0)))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','prewarm','install','resume']);p.add_argument('--client',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True);a=p.parse_args()
    dict(prepare=prepare,prewarm=prewarm,install=install,resume=prior.resume)[a.mode](a.client.resolve(),a.bundle.resolve())
