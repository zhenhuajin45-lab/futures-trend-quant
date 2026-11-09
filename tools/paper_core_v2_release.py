"""Local staged release. Never stop processes, place orders, or clear a risk halt."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,ctypes,hashlib,json,shutil,subprocess,sys
from tools.build_standalone_paper import build
from futures_trend_quant.live.paper_config import frozen_core_digest,load_paper_config
from futures_trend_quant.live.paper_broker import atomic,TERMINAL

ROOT=Path(__file__).resolve().parents[1]


def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def lfsha(p):return hashlib.sha256(Path(p).read_bytes().replace(b'\r\n',b'\n')).hexdigest()


def pid_alive(pid):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
    kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    h=kernel.OpenProcess(0x1000,False,int(pid))
    if not h:
        if ctypes.get_last_error() in (87,1168):return False
        raise RuntimeError('process liveness unavailable; release denied')
    code=ctypes.c_ulong()
    try:
        if not kernel.GetExitCodeProcess(h,ctypes.byref(code)):raise RuntimeError('process state unknown')
        return code.value==259
    finally:kernel.CloseHandle(h)


def prepare(client,bundle):
    active=client/'_strategy_runtime';stage=bundle/'client';rt=stage/'_strategy_runtime'
    if bundle.exists():raise ValueError('release directory must be new')
    rt.mkdir(parents=True);(rt/'config').mkdir()
    c=read(active/'config/paper_observation_five.local.json')
    for name in ('paper_account.local.json','paper_feishu.local.json','paper_platform_auth.local.json','strategy.json'):
        shutil.copyfile(active/'config'/name,rt/'config'/name)
    if c.get('warmup_no_trade_manifest'):
        dest=rt/c['warmup_no_trade_manifest'];dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(active/c['warmup_no_trade_manifest'],dest)
    c.update(strategy_name='S0_SHORT_PAPER_CORE_V2',permission_policy='CLOSED_FACTS_PERMISSION_V1',core_policy_sha256_lf=frozen_core_digest(ROOT))
    atomic(rt/'config/paper_observation_five.local.json',c)
    receipt=build(ROOT,stage/'main.py')
    check=subprocess.run([sys.executable,'-X','utf8',str(stage/'main.py'),'--self-check','--check-config'],capture_output=True,text=True,encoding='utf8',check=True)
    plan=dict(prepared_at=datetime.now(timezone.utc).isoformat(),scope='LOCAL_ONLY_STAGED_NO_PROCESS_CHANGE',
        old_main_sha256_lf=lfsha(client/'main.py'),old_config_sha256=sha(active/'config/paper_observation_five.local.json'),
        new_main_sha256_lf=lfsha(stage/'main.py'),new_config_sha256=sha(rt/'config/paper_observation_five.local.json'),
        new_core_sha256_lf=frozen_core_digest(ROOT),self_check=json.loads(check.stdout.strip().splitlines()[-1]),
        active_client=str(client),staged_client=str(stage),orders_sent=0,restore_new_entries=False)
    atomic(bundle/'release_plan.local.json',plan)
    print(json.dumps(dict(prepared=True,bundle=str(bundle),embedded_files=receipt['embedded_files'],self_check='PASS',active_runtime_modified=False)))


def flat_stopped(client):
    rt=client/'_strategy_runtime';ev=rt/'evidence/paper_observation_five';host=read(ev/'gm_platform_host.local.json')
    if pid_alive(host['pid']):raise ValueError('stop only the reviewed SIM strategy through GM client first')
    s=read(ev/'status.json');e=read(rt/'state/paper_observation_five/execution.json')
    b=s['broker']
    events=[json.loads(x) for x in (ev/'runtime_events.jsonl').read_text(encoding='utf8').splitlines()]
    stop=next((x for x in reversed(events) if x.get('kind')=='GM_PLATFORM_HOST_STOPPED'),None)
    if not stop:raise ValueError('GM shutdown evidence missing')
    if datetime.fromisoformat(stop['at'])<datetime.fromisoformat(host['at']):raise ValueError('flat confirmation belongs to an older host')
    # An exception may leave the shutdown flag UNKNOWN/false. Require a new,
    # account-scoped native snapshot after that stop; never rewrite the stop flag.
    if ((datetime.now(timezone.utc)-datetime.fromisoformat(s['at'])).total_seconds()>45
            or stop.get('flat_confirmed') is not True
            or s.get('config_sha256')!=e.get('config_sha256')):
        path=ev/'controlled_handoff_snapshot.local.json'
        if not path.exists():raise ValueError('handoff account evidence stale')
        handoff=read(path);data=handoff['snapshot']
        if (handoff.get('source')!='GM_NATIVE_SIM_EXPLICIT_ACCOUNT_READ_ONLY'
                or handoff.get('config_sha256')!=sha(rt/'config/paper_observation_five.local.json')
                or datetime.fromisoformat(handoff['received_at'])<datetime.fromisoformat(stop['at'])
                or not 0<=(datetime.now(timezone.utc)-datetime.fromisoformat(handoff['received_at'])).total_seconds()<=30):
            raise ValueError('fresh explicit-account handoff evidence required')
        rows=[data['cash']]+data['positions']+data['orders']+data['active']+data['fills']
        if any(x.get('account_id')!=e['account_id'] for x in rows) or data['active'] or any(int(p['volume'])>0 for p in data['positions']):
            raise ValueError('handoff snapshot foreign/active/nonflat')
        known={i.get('order_id') for i in e['intents'].values() if i.get('order_id')}
        if any(x['cl_ord_id'] not in known for x in data['orders']):raise ValueError('unknown handoff broker order')
        b=dict(b,status='RECONCILED',positions=data['positions'],active_intents=0)
        s=dict(s,broker=b,handoff_account_verified_at=handoff['received_at'])
    if b['status']!='RECONCILED' or b['active_intents'] or any(int(p['volume'])>0 for p in b['positions']):raise ValueError('account not reconciled flat')
    if any(i['status'] not in TERMINAL for i in e['intents'].values()):raise ValueError('uncertain local intent')
    expected={}
    for i in e['intents'].values():
        key=(i['symbol'],i['direction']);expected[key]=expected.get(key,0)+(1 if i['effect']==1 else -1)*int(i.get('filled',0))
    if any(expected.values()):raise ValueError('local cumulative fills not flat')
    if e['halt_reason']:raise ValueError('risk halt cannot be cleared by release')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.restype=ctypes.c_void_p
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    ctypes.set_last_error(0)
    handle=kernel.CreateMutexW(None,False,'Local\\FTQ_GM_PAPER_'+hashlib.sha256(e['account_id'].encode()).hexdigest()[:24])
    exists=ctypes.get_last_error()==183
    if not handle:raise RuntimeError('account controller uniqueness unknown')
    kernel.CloseHandle(handle)
    if exists:raise ValueError('another controller still owns the simulation account')
    return rt,ev,e,s


def install(client,bundle):
    plan=read(bundle/'release_plan.local.json');staged=bundle/'client';rt,ev,e,s=flat_stopped(client)
    if str(client.resolve())!=str(Path(plan['active_client']).resolve()):raise ValueError('release client mismatch')
    if lfsha(client/'main.py')!=plan['old_main_sha256_lf'] or sha(rt/'config/paper_observation_five.local.json')!=plan['old_config_sha256']:
        raise ValueError('active source/profile changed after preparation')
    if lfsha(staged/'main.py')!=plan['new_main_sha256_lf'] or sha(staged/'_strategy_runtime/config/paper_observation_five.local.json')!=plan['new_config_sha256']:
        raise ValueError('staged release changed')
    gate=read(bundle/'validation_gate.local.json')
    if gate.get('tests_passed') is not True or gate.get('gold_replay_passed') is not True or gate.get('core_sha256_lf')!=plan['new_core_sha256_lf']:
        raise ValueError('correctness release gates missing')
    backup=bundle/'rollback';backup.mkdir()
    shutil.copyfile(client/'main.py',backup/'main.py')
    shutil.copytree(rt/'state/paper_observation_five',backup/'state')
    shutil.copyfile(rt/'config/paper_observation_five.local.json',backup/'paper_observation_five.local.json')
    # Preserve cumulative fills, risk peaks, deduplication and prior evidence.
    shutil.copyfile(staged/'main.py',client/'main.py')
    shutil.copyfile(staged/'_strategy_runtime/config/paper_observation_five.local.json',rt/'config/paper_observation_five.local.json')
    for p in (rt/'state/paper_observation_five/products').glob('*/core_checkpoint.json'):
        p.rename(p.with_name('legacy_dfed9d6a_core_checkpoint.json'))
    for p in (rt/'state/paper_observation_five/products').glob('*/contract_pin.json'):
        pin=read(p);pin['config_sha256']=plan['new_config_sha256'];atomic(p,pin)
    e.update(config_sha256=plan['new_config_sha256'],pause_new=True)
    atomic(rt/'state/paper_observation_five/execution.json',e)
    atomic(rt/'state/paper_observation_five/migration.local.json',dict(
        kind='CORE_V2_FLAT_ONLY_REBUILD',at=datetime.now(timezone.utc).isoformat(),
        old_config_sha256=plan['old_config_sha256'],new_config_sha256=plan['new_config_sha256'],
        new_core_sha256_lf=plan['new_core_sha256_lf'],broker_checked=True,positions=0,active_intents=0,
        pause_preserved=True,baseline_archived=True,self_check_only=True))
    print(json.dumps(dict(installed=True,pause_new=True,rollback=str(backup),orders_sent=0)))


def record_resume_authorization(e,plan,host,stop,authorization,now):
    """Record an operator decision; a new stop requires a new scoped decision."""
    key=plan['new_config_sha256']
    if authorization is None:
        if key in e.setdefault('core_v2_resume_authorizations',{}):raise ValueError('this one-time restart authorization was consumed')
        e['core_v2_resume_authorizations'][key]=dict(at=now.isoformat(),basis='USER_CORE_REPAIR_TASK_AFTER_CONCRETE_GATES',review='SINGLE_AGENT_SELF_CHECK')
        return
    a=authorization
    issued=datetime.fromisoformat(a['issued_at']);expires=datetime.fromisoformat(a['expires_at'])
    if not issued<=now<expires or not 0<(expires-issued).total_seconds()<=600:
        raise ValueError('operator recovery authorization expired or invalid')
    if (a['stop_at']!=stop['at'] or a['host_pid']!=host['pid']
            or a['main_sha256_lf']!=plan['new_main_sha256_lf'] or a['config_sha256']!=key
            or not a.get('basis') or not a.get('token')
            or not e.get('pause_new') or e.get('halt_reason')
            or not (e.get('pause_metadata',{}).get('reason')=='GM_HOST_SHUTDOWN'
                    or (e.get('pause_metadata',{}).get('reason')=='REVIEWED_RUNTIME_V6_HANDOFF'
                        and e.get('pause_metadata',{}).get('source')=='EXPLICIT_RELEASE'))):
        raise ValueError('operator recovery authorization scope mismatch')
    identity=hashlib.sha256((str(host['pid'])+'|'+stop['at']+'|'+key).encode()).hexdigest()
    token=hashlib.sha256(a['token'].encode()).hexdigest()
    used=e.setdefault('operator_resume_authorizations',{})
    if identity in used or any(x['token_sha256']==token for x in used.values()):
        raise ValueError('operator recovery authorization consumed')
    used[identity]=dict(at=now.isoformat(),token_sha256=token,basis=a['basis'],stop_at=stop['at'],
        host_pid=host['pid'],main_sha256_lf=a['main_sha256_lf'],config_sha256=key,expires_at=a['expires_at'])


def resume(client,bundle,authorization=None):
    """One operator-invoked flat restart after all post-install gates pass.

    No running controller is modified and no halt is cleared. Any subsequent
    platform error pauses again and this authorization cannot be reused.
    """
    rt,ev,e,s=flat_stopped(client);plan=read(bundle/'release_plan.local.json')
    c=load_paper_config(rt/'config/paper_observation_five.local.json',root=rt)
    if c['strategy_name']!='S0_SHORT_PAPER_CORE_V2' or c['core_policy_sha256_lf']!=plan['new_core_sha256_lf']:
        raise ValueError('resume version mismatch')
    host=read(ev/'gm_platform_host.local.json')
    if (s.get('config_sha256')!=c['config_sha256'] or e['config_sha256']!=c['config_sha256']
            or host['config_sha256']!=c['config_sha256'] or lfsha(client/'main.py')!=plan['new_main_sha256_lf']):
        raise ValueError('runtime/source/profile handoff mismatch')
    if set(s['products'])!=set(c['universe']):raise ValueError('observation pool coverage mismatch')
    if c.get('notification_policy_version')=='ACTIONABLE_CARDS_V4_20261106':
        connection=s.get('platform_connections',{}).get('market',{})
        manifest=read(ev/'run_manifest.json')
        if connection.get('status')!='CONNECTED' or connection.get('run_id')!=manifest['run_id']:
            raise ValueError('current host market connection has not been confirmed')
    if s['ready_products']!=5 or not all(p['ready'] and not p['frozen'] and not p['fault'] for p in s['products'].values()):
        raise ValueError('all five product facts must qualify before resume')
    facts_fresh(rt,ev,s)
    if not s['notifications']['configured'] or not s['notifications']['worker_alive']:raise ValueError('notification sender unhealthy')
    h=read(ev/'trend_reminder_health.local.json')
    events=[json.loads(x) for x in (ev/'runtime_events.jsonl').read_text(encoding='utf8').splitlines()]
    starts=[x for x in events if x.get('kind')=='TREND_NOTIFIER_STARTED']
    if not starts or starts[-1]['pid']!=h['pid']:raise ValueError('notification evidence not from owned child')
    stop=next(x for x in reversed(events) if x.get('kind')=='GM_PLATFORM_HOST_STOPPED')
    notifier_health_at_stop(h,starts[-1],stop,flat_proof_at=s.get('handoff_account_verified_at'))
    now=datetime.now(timezone.utc)
    if any(x.get('kind') in ('GM_PLATFORM_ERROR','GM_PLATFORM_CYCLE_FAULT','EXECUTION_FAULT','NOTIFICATION_HEALTH_FAULT')
           and (now-datetime.fromisoformat(x['at'])).total_seconds()<=60 for x in events):
        raise ValueError('recent fault blocks controlled resume')
    control=rt/'state/paper_observation_five/control.json'
    if control.exists() and read(control).get('stop_new_open'):raise ValueError('operator stop control still active')
    # Recheck the same stopped/account gates immediately before the state write;
    # a slow facts check must not turn an old snapshot into current authority.
    _,_,e,_=flat_stopped(client)
    current_host=read(ev/'gm_platform_host.local.json')
    current_stop=next(json.loads(x) for x in reversed((ev/'runtime_events.jsonl').read_text(encoding='utf8').splitlines()) if json.loads(x).get('kind')=='GM_PLATFORM_HOST_STOPPED')
    if current_host!=host or current_stop!=stop:raise ValueError('host/stop changed during controlled recovery')
    record_resume_authorization(e,plan,host,stop,authorization,datetime.now(timezone.utc))
    e['pause_new']=False
    atomic(rt/'state/paper_observation_five/execution.json',e)
    receipt=bundle/('controlled_resume.local.json' if authorization is None else 'controlled_resume_'+hashlib.sha256(authorization['token'].encode()).hexdigest()[:16]+'.local.json')
    atomic(receipt,dict(at=now.isoformat(),current_controller_stopped=True,
        account_flat=True,active_intents=0,core_sha256_lf=plan['new_core_sha256_lf'],facts_qualified=5,
        notifier_health_at=h['at'],notifier_verified_at_stop=stop['at'],
        authorization_basis=authorization['basis'] if authorization else 'USER_CORE_REPAIR_TASK_AFTER_CONCRETE_GATES',
        release_utility_sha256_lf=lfsha(Path(__file__)),orders_sent=0,subsequent_errors_pause_again=True))
    print(json.dumps(dict(ready_for_one_controlled_restart=True,cleared_only_operator_pause=True,risk_halt_cleared=False,orders_sent=0)))


def notifier_health_at_stop(health,start,stop,flat_proof_at=None):
    """Owned child stops with its host; prove health at shutdown, never refresh it.

    Current account/source/facts freshness is checked separately. This is not an
    exemption for a notifier that had failed before the deliberate shutdown.
    """
    received=datetime.fromisoformat(health['at']);started=datetime.fromisoformat(start['at'])
    stopped=datetime.fromisoformat(stop['at'])
    # flat_stopped validates account, source, profile, orders and timestamp before
    # exposing this proof. Do not contradict its verified post-stop flat result.
    flat=stop.get('flat_confirmed') is True
    if not flat and flat_proof_at:
        proof=datetime.fromisoformat(flat_proof_at);now=datetime.now(timezone.utc)
        flat=stopped<=proof<=now and 0<=(now-proof).total_seconds()<=30
    if (start['pid']!=health['pid'] or not flat
            or not started<=received<=stopped or not 0<=(stopped-received).total_seconds()<=30):
        raise ValueError('trend notifier not healthy at reviewed shutdown')


def facts_fresh(rt,ev,status):
    import pandas as pd
    from futures_trend_quant.live.paper_calendar import PaperCalendar
    cutoff=pd.Timestamp.now(tz='Asia/Shanghai')-pd.Timedelta(seconds=5)
    for cs,p in status['products'].items():
        meta=read(ev/'products'/cs.replace('.','_')/'market_metadata.local.json')
        # Only recent sessions are needed for the latest expected closure, not
        # months of indicator warmup. Keep six previous and three future trading
        # dates so night ownership and long-holiday gaps retain the same meaning.
        dates=sorted(set(str(x)[:10] for x in meta['dates']));day=cutoff.date().isoformat()
        recent=[d for d in dates if d<=day][-6:]+[d for d in dates if d>day][:3]
        cal=PaperCalendar(recent,meta['sessions'],cs)
        elapsed=cal.clock[cal.clock<=cutoff]
        ends=elapsed[(elapsed.asi8//1_000_000_000)%900==0]
        if not len(ends) or pd.Timestamp(p['core']['last_eob'])<ends[-1]:raise ValueError('closed M15 core behind market: '+cs)
        if cal.market_open(cutoff) and pd.Timestamp(p['core']['last_closed_m1'])<elapsed[-1]-pd.Timedelta(minutes=1):
            raise ValueError('M1 source behind market: '+cs)


def refresh_warmup(client,bundle):
    """Same-core adapter refresh; preserve partial V2 facts and exact original seed."""
    rt,ev,e,s=flat_stopped(client);plan=read(bundle/'release_plan.local.json')
    if (sha(rt/'config/paper_observation_five.local.json')!=plan['old_config_sha256']
            or plan['old_config_sha256']!=plan['new_config_sha256']
            or lfsha(client/'main.py')!=plan['old_main_sha256_lf']):
        raise ValueError('same-profile adapter refresh required')
    if frozen_core_digest(rt)!=plan['new_core_sha256_lf']:raise ValueError('adapter refresh cannot alter core')
    newmain=bundle/'client/main.py'
    if lfsha(newmain)!=plan['new_main_sha256_lf']:raise ValueError('new adapter bundle changed')
    proof={};checkpoints={}
    from futures_trend_quant.live.paper_data import MarketWorker,complete_bars
    for cs,p in s['products'].items():
        cp_path=rt/'state/paper_observation_five/products'/cs.replace('.','_')/'core_checkpoint.json'
        cp=read(cp_path)
        if cp['runtime'].get('core_contract')!='CLOSED_FACTS_V2':raise ValueError('partial state is not V2')
        if cp.get('seed_cache'):
            seed=cp['seed_cache']
        else:
            candidates=[]
            for folder in (rt/'data/cache/paper_market').glob(cs.replace('.','_')+'_*'):
                m=read(folder/'input_manifest.json')
                if m['symbol']==p['symbol']:candidates.append((datetime.fromisoformat(m['requested_at']),folder,m))
            _,folder,m=max(candidates,key=lambda x:x[0])
            seed=dict(path=str(folder/'initial_m1.parquet'),sha256=m['sha256'],symbol=m['symbol'],cutoff=m['requested_at'])
        path=Path(seed['path']).resolve()
        if not path.is_relative_to(rt/'data/cache/paper_market') or sha(path)!=seed['sha256']:raise ValueError('seed proof changed')
        import pandas as pd
        loader=MarketWorker(dict(evidence_dir=str(ev/'products'/cs.replace('.','_'))),child=True);loader.symbol=seed['symbol']
        bars=loader.bars(pd.read_parquet(path),seed['cutoff'])
        expected={b.identity for b in complete_bars(bars,15) if b.eob<=datetime.fromisoformat(cp['last_eob'])}
        observed=set(cp['runtime']['facts']['by_csymbol'][cs]['observed_bar_ids'])
        if expected!=observed:raise ValueError('partial facts do not match exact frozen seed prefix: '+cs)
        proof[cs]=dict(seed_sha256=seed['sha256'],prefix_m15=len(expected),checkpoint_before_sha256=sha(cp_path))
        cp['seed_cache']=seed;checkpoints[cp_path]=cp
    backup=bundle/'rollback_before_adapter';backup.mkdir()
    shutil.copyfile(client/'main.py',backup/'main.py');shutil.copytree(rt/'state/paper_observation_five',backup/'state')
    shutil.copyfile(newmain,client/'main.py')
    for path,cp in checkpoints.items():atomic(path,cp)
    subprocess.run([sys.executable,'-X','utf8',str(client/'main.py'),'--self-check','--check-config'],check=True)
    atomic(bundle/'adapter_refresh.local.json',dict(at=datetime.now(timezone.utc).isoformat(),core_unchanged=True,
        pause_preserved=True,confirmed_facts_unchanged=True,seed_prefix_proofs=proof,orders_sent=0))
    print(json.dumps(dict(refreshed=True,partial_fact_books_preserved=5,exact_frozen_seed_prefixes=5,pause_new=True,orders_sent=0)))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','install','resume','refresh-warmup']);p.add_argument('--client',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True)
    a=p.parse_args();dict(prepare=prepare,install=install,resume=resume,**{'refresh-warmup':refresh_warmup})[a.mode](a.client.resolve(),a.bundle.resolve())
