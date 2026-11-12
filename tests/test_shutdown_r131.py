"""Normal user-stop callback -> real worker final proof -> unchanged 30s recovery guard."""
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import importlib.util,json,subprocess,sys,time,hashlib
import pandas as pd
import pytest
from futures_trend_quant.live import gm_paper_host as host
from tools.paper_core_v2_release import notifier_health_at_stop,notifier_shutdown_proof


def producer(path):
    # Deliberately real child work and source clocks. No SDK/account/order APIs.
    code='''import os,json,time,tempfile
from pathlib import Path
from datetime import datetime,timezone
p=Path(__import__('sys').argv[1]);n=0
while True:
 n+=1;t=datetime.now(timezone.utc).isoformat();r=dict(pid=os.getpid(),policy='SYNTHETIC_REAL_WORK',sequence=n,status='COMPLETED',stage='TEST',stage_started_at=t,last_completed_at=t,at=t,stage_failures={},trade_api_called=False)
 fd,tmp=tempfile.mkstemp(dir=p.parent)
 with os.fdopen(fd,'w') as f:json.dump(r,f)
 os.replace(tmp,p);time.sleep(.01)
'''
    process=subprocess.Popen([sys.executable,'-c',code,str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    for i in range(100):
        if path.exists():return process
        time.sleep(.02)
    process.terminate();raise RuntimeError('TEST_CHILD_NOT_READY')


class SlowWorker:
    def __init__(self):self.alive=True
    def is_alive(self):return self.alive
    def terminate(self):self.alive=False
    def join(self,timeout):time.sleep(.3)


@pytest.mark.parametrize('old',[True,False])
def test_real_owned_notifier_cannot_overwrite_proof_after_final_stopped_receipt(tmp_path,monkeypatch,old):
    module=host
    if old:
        path=Path(__file__).parents[1]/'evidence/releases/gm_execution_r13_20261112/client/_strategy_runtime/futures_trend_quant/live/gm_paper_host.py'
        if not path.exists():pytest.skip('Local frozen R13 original is not a public test fixture')
        spec=importlib.util.spec_from_file_location('futures_trend_quant.live._frozen_r13_host',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'timer_stop',Mock())
    began=datetime.now(timezone.utc).isoformat();path=tmp_path/'health.json';child=producer(path);events=[];state=dict(pause_new=False,halt_reason='KEEP')
    def pause(*a,**kw):state['pause_new']=True
    def event(kind,**kw):events.append(dict(kind=kind,at=datetime.now(timezone.utc).isoformat(),**kw))
    engine=SimpleNamespace(pause_entries=pause,ready=True,last_snapshot=pd.Timestamp.now(tz='Asia/Shanghai'),positions=lambda:[],active=lambda:[])
    transport=SimpleNamespace(shutdown=lambda:time.sleep(.05))
    r=SimpleNamespace(engine=engine,readonly=False,run_id='TEST',c=dict(config_sha256='C'),evidence=tmp_path,event=event,notice=Mock(),quote_worker=SlowWorker(),products={},notifications=None,trend_notifier=child,transport=transport)
    if not old:
        # The real producer publishes the exact production filename.
        child.terminate();child.wait(timeout=5);path=tmp_path/'trend_reminder_health.local.json';child=producer(path);r.trend_notifier=child
    context=SimpleNamespace(ftq_paper_runner=r,ftq_paper_timer_id=1,ftq_paper_stopped=False)
    try:
        module.on_shutdown(context);child.wait(timeout=5);health=json.loads(path.read_text());stop=next(r for r in events if r['kind']=='GM_PLATFORM_HOST_STOPPED')
        if old:
            assert datetime.fromisoformat(health['at'])>datetime.fromisoformat(stop['at'])
            with pytest.raises(ValueError):notifier_health_at_stop(health,dict(pid=child.pid,at=began),stop)
        else:
            assert events[0]['kind']=='GM_PLATFORM_HOST_STOPPING' and events[-1]['kind']=='GM_PLATFORM_HOST_STOPPED'
            assert datetime.fromisoformat(health['at'])<=datetime.fromisoformat(stop['at'])
            assert stop['notifier_exited'] and stop['remaining_owned_workers']==0 and stop['shutdown_policy']==host.SHUTDOWN_POLICY
            notifier_health_at_stop(health,dict(pid=child.pid,at=began),stop)
            frozen=notifier_shutdown_proof(tmp_path,dict(pid=child.pid,at=began),stop,dict(config_sha256='C'),dict(run_id='TEST'))
            assert frozen['pid']==child.pid and stop['notifier_stop_proof']['sha256']==hashlib.sha256((tmp_path/stop['notifier_stop_proof']['file']).read_bytes()).hexdigest()
        assert state==dict(pause_new=True,halt_reason='KEEP')
        count=len(events);module.on_shutdown(context);assert len(events)==count
    finally:
        if child.poll() is None:child.terminate();child.wait(timeout=5)


@pytest.mark.parametrize('bad',['future','stale','pid','failed','unfinished','failed_stage','notifier_alive','worker_alive'])
def test_all_original_progress_clock_and_ownership_limits_remain_strict(bad):
    now=datetime.now(timezone.utc);health=dict(at=(now-timedelta(seconds=1)).isoformat(),pid=1,status='COMPLETED',last_completed_at=(now-timedelta(seconds=1)).isoformat(),stage_failures={})
    start=dict(pid=1,at=(now-timedelta(minutes=2)).isoformat());stop=dict(at=now.isoformat(),flat_confirmed=True,shutdown_policy=host.SHUTDOWN_POLICY,notifier_exited=True,remaining_owned_workers=0)
    if bad=='future':health['at']=(now+timedelta(microseconds=1)).isoformat()
    elif bad=='stale':health['at']=(now-timedelta(seconds=30.001)).isoformat()
    elif bad=='pid':health['pid']=2
    elif bad=='failed':health['status']='FAILED'
    elif bad=='unfinished':health.update(status='RUNNING',stage='HUNG',stage_started_at=(now-timedelta(seconds=31)).isoformat())
    elif bad=='failed_stage':health['stage_failures']={'BROKEN':{'reason':'TEST'}}
    elif bad=='notifier_alive':stop['notifier_exited']=False
    elif bad=='worker_alive':stop['remaining_owned_workers']=1
    with pytest.raises(ValueError):notifier_health_at_stop(health,start,stop)


def test_failed_teardown_never_emits_final_stopped_or_clears_protection(monkeypatch):
    monkeypatch.setattr(host,'timer_stop',Mock());events=[]
    def fail():raise RuntimeError('SYNTHETIC_TEARDOWN_FAILURE')
    r=SimpleNamespace(engine=None,readonly=True,event=lambda kind,**kw:events.append(kind),notice=Mock(),quote_worker=SlowWorker(),products={},notifications=None,transport=SimpleNamespace(shutdown=fail))
    c=SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False)
    with pytest.raises(RuntimeError,match='TEARDOWN'):host.on_shutdown(c)
    assert events==['GM_PLATFORM_HOST_STOPPING']


@pytest.mark.parametrize('case',['valid','latest_corrupt','hash','run','config','path','future','failed_stage','missing'])
def test_frozen_stop_proof_is_identity_clock_and_byte_bound(tmp_path,case):
    now=datetime.now(timezone.utc);at=(now-timedelta(seconds=1)).isoformat();body=dict(pid=1,at=at,status='COMPLETED',last_completed_at=at,stage_failures={})
    if case=='future':body['at']=(now+timedelta(seconds=1)).isoformat()
    if case=='failed_stage':body['stage_failures']={'BROKEN':{'reason':'TEST'}}
    path=tmp_path/'shutdown_proofs/real.json';path.parent.mkdir();raw=json.dumps(body).encode();path.write_bytes(raw)
    proof=dict(policy=host.SHUTDOWN_POLICY,file=path.relative_to(tmp_path).as_posix(),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),notifier_pid=1,run_id='R',config_sha256='C',checked_at=at)
    stop=dict(at=now.isoformat(),flat_confirmed=True,shutdown_policy=host.SHUTDOWN_POLICY,notifier_exited=True,remaining_owned_workers=0,notifier_stop_proof=proof)
    if case=='latest_corrupt':(tmp_path/'trend_reminder_health.local.json').write_bytes(b'BAD_CURRENT_FILE')
    elif case=='hash':path.write_bytes(b'CHANGED')
    elif case=='run':proof['run_id']='OLD'
    elif case=='config':proof['config_sha256']='OLD'
    elif case=='path':proof['file']='../outside.json'
    elif case=='missing':stop['notifier_stop_proof']=None
    start=dict(pid=1,at=(now-timedelta(minutes=1)).isoformat())
    if case in ('valid','latest_corrupt'):assert notifier_shutdown_proof(tmp_path,start,stop,dict(config_sha256='C'),dict(run_id='R'))==body
    else:
        with pytest.raises((ValueError,OSError)):notifier_shutdown_proof(tmp_path,start,stop,dict(config_sha256='C'),dict(run_id='R'))


@pytest.mark.parametrize('case',['valid','flat_unknown','tampered','changed_before_state_write','wrong_loaded_source'])
def test_new_stop_envelope_reaches_real_single_use_state_write_with_all_other_guards(tmp_path,monkeypatch,case):
    from tools import paper_core_v2_release as base
    from test_paper_v2_release import fixture
    from test_symbol_market_isolation import PAIRS
    from futures_trend_quant.live.paper_symbol_health import POLICY as ISOLATION
    from futures_trend_quant.live.paper_tq import POLICY as TQ
    rt,ev,st,put=fixture(tmp_path,monkeypatch);now=datetime.now(timezone.utc)
    (tmp_path/'main.py').write_text('SYNTHETIC_REVIEWED_SOURCE');(rt/'config').mkdir();profile=rt/'config/paper_observation_five.local.json';put(profile,{'synthetic':'reviewed'})
    digest=base.sha(profile);config=dict(strategy_name='S0_SHORT_PAPER_CORE_V2',core_policy_sha256_lf='CORE',config_sha256=digest,universe=[cs for cs,s in PAIRS],symbol_market_policy=ISOLATION,external_market_policy=TQ,notification_policy_version='ACTIONABLE_CARDS_V4_20261106')
    monkeypatch.setattr(base,'load_paper_config',lambda *a,**k:config);monkeypatch.setattr(base,'facts_fresh',lambda *a:None)
    healthy={cs for cs,s in PAIRS[:5]};status=base.read(ev/'status.json');status.update(config_sha256=digest,ready_products=5,
        products={cs:dict(symbol=s,ready=cs in healthy,frozen=cs not in healthy,fault=None if cs in healthy else 'SECONDARY_SOURCE_DOWN') for cs,s in PAIRS},
        symbol_entry_readiness={s:dict(market_proof=dict(symbol=s,ready=cs in healthy)) for cs,s in PAIRS},notifications=dict(configured=True,worker_alive=True),
        platform_connections={k:dict(status='CONNECTED',run_id='R') for k in ('market','host_trade')})
    put(ev/'status.json',status);put(ev/'run_manifest.json',dict(run_id='R'));state=base.read(st/'execution.json');state.update(config_sha256=digest,pause_metadata=dict(reason='GM_HOST_SHUTDOWN'));put(st/'execution.json',state)
    host_receipt=base.read(ev/'gm_platform_host.local.json');host_receipt.update(config_sha256=digest,entry_sha256_lf='WRONG' if case=='wrong_loaded_source' else base.lfsha(tmp_path/'main.py'));put(ev/'gm_platform_host.local.json',host_receipt)
    body=dict(pid=77,at=(now-timedelta(seconds=2)).isoformat(),last_completed_at=(now-timedelta(seconds=2)).isoformat(),status='COMPLETED',stage_failures={},sequence=44)
    file=ev/'shutdown_proofs/sealed.json';file.parent.mkdir();raw=json.dumps(body).encode();file.write_bytes(raw)
    stop=dict(kind='GM_PLATFORM_HOST_STOPPED',at=(now-timedelta(seconds=.5)).isoformat(),flat_confirmed=case!='flat_unknown',shutdown_policy=host.SHUTDOWN_POLICY,notifier_exited=True,remaining_owned_workers=0,
        notifier_stop_proof=dict(policy=host.SHUTDOWN_POLICY,file='shutdown_proofs/sealed.json',bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),checked_at=(now-timedelta(seconds=1)).isoformat(),notifier_pid=77,run_id='R',config_sha256=digest))
    with (ev/'runtime_events.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(kind='TREND_NOTIFIER_STARTED',pid=77,at=(now-timedelta(seconds=60)).isoformat()))+'\n');stream.write(json.dumps(stop)+'\n')
    if case=='flat_unknown':put(ev/'controlled_handoff_snapshot.local.json',dict(source='GM_NATIVE_SIM_EXPLICIT_ACCOUNT_READ_ONLY',config_sha256=digest,received_at=now.isoformat(),snapshot=dict(cash=dict(account_id=state['account_id']),positions=[],orders=[],active=[],fills=[])))
    bundle=tmp_path/'bundle';bundle.mkdir();plan=dict(new_config_sha256=digest,new_core_sha256_lf='CORE',new_main_sha256_lf=base.lfsha(tmp_path/'main.py'),policies=dict(shutdown=host.SHUTDOWN_POLICY));put(bundle/'release_plan.local.json',plan)
    auth=dict(token='SYNTHETIC_SINGLE_AUTH',basis='SYNTHETIC_EXPLICIT_USER',issued_at=(now-timedelta(seconds=.1)).isoformat(),expires_at=(now+timedelta(minutes=5)).isoformat(),stop_at=stop['at'],host_pid=host_receipt['pid'],main_sha256_lf=plan['new_main_sha256_lf'],config_sha256=digest)
    if case=='tampered':file.write_bytes(b'CHANGED')
    if case=='changed_before_state_write':
        original=base.flat_stopped;calls=[]
        def checked(*a):
            result=original(*a);calls.append(True)
            if len(calls)==2:file.write_bytes(b'CHANGED')
            return result
        monkeypatch.setattr(base,'flat_stopped',checked)
    if case in ('valid','flat_unknown'):
        base.resume(tmp_path,bundle,authorization=auth);after=base.read(st/'execution.json')
        assert not after['pause_new'] and len(after['operator_resume_authorizations'])==1
        assert {k:v for k,v in after.items() if k not in ('pause_new','operator_resume_authorizations')}=={k:v for k,v in state.items() if k not in ('pause_new','operator_resume_authorizations')}
        assert base.read(next(bundle.glob('controlled_resume_*.local.json')))['facts_qualified']==5
    else:
        with pytest.raises(ValueError):base.resume(tmp_path,bundle,authorization=auth)
        assert base.read(st/'execution.json')==state
