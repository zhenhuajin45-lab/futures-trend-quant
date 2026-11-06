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
    if (datetime.now(timezone.utc)-datetime.fromisoformat(s['at'])).total_seconds()>45:raise ValueError('handoff account evidence stale')
    b=s['broker']
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
    events=[json.loads(x) for x in (ev/'runtime_events.jsonl').read_text(encoding='utf8').splitlines()]
    stop=next((x for x in reversed(events) if x.get('kind')=='GM_PLATFORM_HOST_STOPPED'),None)
    if not stop or stop.get('flat_confirmed') is not True:raise ValueError('GM shutdown did not confirm broker flat')
    if datetime.fromisoformat(stop['at'])<datetime.fromisoformat(host['at']):raise ValueError('flat confirmation belongs to an older host')
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


def resume(client,bundle):
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
    if s['ready_products']!=5 or not all(p['ready'] and not p['frozen'] and not p['fault'] for p in s['products'].values()):
        raise ValueError('all five product facts must qualify before resume')
    if not s['notifications']['configured'] or not s['notifications']['worker_alive']:raise ValueError('notification sender unhealthy')
    h=read(ev/'trend_reminder_health.local.json')
    if (datetime.now(timezone.utc)-datetime.fromisoformat(h['at'])).total_seconds()>30:raise ValueError('trend notifier health stale')
    events=[json.loads(x) for x in (ev/'runtime_events.jsonl').read_text(encoding='utf8').splitlines()]
    starts=[x for x in events if x.get('kind')=='TREND_NOTIFIER_STARTED']
    if not starts or starts[-1]['pid']!=h['pid']:raise ValueError('notification evidence not from owned child')
    now=datetime.now(timezone.utc)
    if any(x.get('kind') in ('GM_PLATFORM_ERROR','GM_PLATFORM_CYCLE_FAULT','EXECUTION_FAULT','NOTIFICATION_HEALTH_FAULT')
           and (now-datetime.fromisoformat(x['at'])).total_seconds()<=60 for x in events):
        raise ValueError('recent fault blocks controlled resume')
    control=rt/'state/paper_observation_five/control.json'
    if control.exists() and read(control).get('stop_new_open'):raise ValueError('operator stop control still active')
    key=plan['new_config_sha256']
    if key in e.setdefault('core_v2_resume_authorizations',{}):raise ValueError('this one-time restart authorization was consumed')
    e['core_v2_resume_authorizations'][key]=dict(at=now.isoformat(),basis='USER_CORE_REPAIR_TASK_AFTER_CONCRETE_GATES',review='SINGLE_AGENT_SELF_CHECK')
    e['pause_new']=False
    atomic(rt/'state/paper_observation_five/execution.json',e)
    atomic(bundle/'controlled_resume.local.json',dict(at=now.isoformat(),current_controller_stopped=True,
        account_flat=True,active_intents=0,core_sha256_lf=plan['new_core_sha256_lf'],facts_qualified=5,
        orders_sent=0,subsequent_errors_pause_again=True))
    print(json.dumps(dict(ready_for_one_controlled_restart=True,cleared_only_operator_pause=True,risk_halt_cleared=False,orders_sent=0)))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','install','resume']);p.add_argument('--client',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True)
    a=p.parse_args();dict(prepare=prepare,install=install,resume=resume)[a.mode](a.client.resolve(),a.bundle.resolve())
