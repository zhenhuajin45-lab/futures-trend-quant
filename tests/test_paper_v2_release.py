"""Release gates are exercised on temporary files, never a real controller."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import json,os
import pytest
from tools import paper_core_v2_release as release


def fixture(tmp_path,monkeypatch):
    rt=tmp_path/'_strategy_runtime';ev=rt/'evidence/paper_observation_five';st=rt/'state/paper_observation_five'
    ev.mkdir(parents=True);st.mkdir(parents=True);now=datetime.now(timezone.utc)
    def put(p,r):p.write_text(json.dumps(r),encoding='utf8')
    put(ev/'gm_platform_host.local.json',dict(pid=123,at=(now-timedelta(minutes=2)).isoformat()))
    put(ev/'status.json',dict(at=now.isoformat(),broker=dict(status='RECONCILED',active_intents=0,positions=[])))
    e=dict(account_id='00000000-0000-0000-0000-000000000001',intents={},halt_reason='',pause_new=True)
    put(st/'execution.json',e)
    (ev/'runtime_events.jsonl').write_text(json.dumps(dict(at=now.isoformat(),kind='GM_PLATFORM_HOST_STOPPED',flat_confirmed=True))+'\n',encoding='utf8')
    monkeypatch.setattr(release,'pid_alive',lambda _:False)
    return rt,ev,st,put


def test_running_host_cannot_be_replaced(tmp_path,monkeypatch):
    fixture(tmp_path,monkeypatch);monkeypatch.setattr(release,'pid_alive',lambda _:True)
    with pytest.raises(ValueError,match='stop only'):release.flat_stopped(tmp_path)


@pytest.mark.parametrize('case',['stale','position','active','unknown_intent','halt','old_stop','unbalanced_fill'])
def test_release_refuses_any_unknown_or_unflat_handoff(tmp_path,monkeypatch,case):
    rt,ev,st,put=fixture(tmp_path,monkeypatch)
    s=release.read(ev/'status.json');e=release.read(st/'execution.json')
    if case=='stale':s['at']=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
    if case=='position':s['broker']['positions']=[{'volume':1}]
    if case=='active':s['broker']['active_intents']=1
    if case=='unknown_intent':e['intents']={'x':dict(status=10)}
    if case=='halt':e['halt_reason']='DAILY_LOSS_LIMIT'
    if case=='old_stop':
        (ev/'runtime_events.jsonl').write_text(json.dumps(dict(at=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),kind='GM_PLATFORM_HOST_STOPPED',flat_confirmed=True))+'\n',encoding='utf8')
    if case=='unbalanced_fill':e['intents']={'x':dict(status=3,symbol='DCE.m2701',direction='bull',effect=1,filled=1)}
    put(ev/'status.json',s);put(st/'execution.json',e)
    with pytest.raises((ValueError,RuntimeError)):release.flat_stopped(tmp_path)


def test_windows_liveness_query_does_not_terminate_process():
    assert release.pid_alive(os.getpid()) is True


@pytest.mark.parametrize('case',['valid','late','future','foreign','position','active','unknown_order','profile'])
def test_stopped_host_requires_fresh_scoped_readonly_handoff_when_status_stale(tmp_path,monkeypatch,case):
    rt,ev,st,put=fixture(tmp_path,monkeypatch);now=datetime.now(timezone.utc)
    (rt/'config').mkdir();profile=rt/'config/paper_observation_five.local.json';put(profile,{'private':'synthetic'})
    s=release.read(ev/'status.json');s['at']=(now-timedelta(minutes=10)).isoformat();put(ev/'status.json',s)
    account=release.read(st/'execution.json')['account_id']
    data=dict(cash={'account_id':account},positions=[],orders=[],active=[],fills=[])
    proof=dict(source='GM_NATIVE_SIM_EXPLICIT_ACCOUNT_READ_ONLY',config_sha256=release.sha(profile),received_at=now.isoformat(),snapshot=data)
    if case=='late':proof['received_at']=(now-timedelta(seconds=31)).isoformat()
    if case=='future':proof['received_at']=(now+timedelta(seconds=30)).isoformat()
    if case=='foreign':data['cash']['account_id']='OTHER_ACCOUNT'
    if case=='position':data['positions']=[dict(account_id=account,volume=1)]
    if case=='active':data['active']=[dict(account_id=account)]
    if case=='unknown_order':data['orders']=[dict(account_id=account,cl_ord_id='UNKNOWN')]
    if case=='profile':proof['config_sha256']='DIFFERENT_PROFILE'
    put(ev/'controlled_handoff_snapshot.local.json',proof)
    if case=='valid':
        result=release.flat_stopped(tmp_path)
        assert result[-1]['handoff_account_verified_at']==proof['received_at']
        assert release.read(ev/'status.json')['at']==s['at']
    else:
        with pytest.raises(ValueError):release.flat_stopped(tmp_path)


@pytest.mark.parametrize('case',['valid','before_stop','active','foreign','unknown_order','late','risk_halt'])
def test_unconfirmed_shutdown_needs_fresh_post_stop_native_truth_without_rewriting_history(tmp_path,monkeypatch,case):
    rt,ev,st,put=fixture(tmp_path,monkeypatch);now=datetime.now(timezone.utc)
    (rt/'config').mkdir();profile=rt/'config/paper_observation_five.local.json';put(profile,{'private':'synthetic'})
    stop=dict(at=(now-timedelta(seconds=2)).isoformat(),kind='GM_PLATFORM_HOST_STOPPED',flat_confirmed=False)
    (ev/'runtime_events.jsonl').write_text(json.dumps(stop)+'\n',encoding='utf8');account=release.read(st/'execution.json')['account_id']
    data=dict(cash={'account_id':account},positions=[],orders=[],active=[],fills=[])
    proof=dict(source='GM_NATIVE_SIM_EXPLICIT_ACCOUNT_READ_ONLY',config_sha256=release.sha(profile),received_at=now.isoformat(),snapshot=data)
    if case=='before_stop':proof['received_at']=(now-timedelta(seconds=3)).isoformat()
    if case=='active':data['active']=[dict(account_id=account)]
    if case=='foreign':data['cash']['account_id']='FOREIGN'
    if case=='unknown_order':data['orders']=[dict(account_id=account,cl_ord_id='UNKNOWN')]
    if case=='late':proof['received_at']=(now-timedelta(seconds=31)).isoformat()
    if case=='risk_halt':e=release.read(st/'execution.json');e['halt_reason']='DAILY_LOSS_LIMIT';put(st/'execution.json',e)
    put(ev/'controlled_handoff_snapshot.local.json',proof)
    if case=='valid':
        assert release.flat_stopped(tmp_path)[-1]['handoff_account_verified_at']==proof['received_at']
        assert json.loads((ev/'runtime_events.jsonl').read_text())['flat_confirmed'] is False
        assert release.read(st/'execution.json')['pause_new'] is True
    else:
        with pytest.raises(ValueError):release.flat_stopped(tmp_path)


def test_controlled_resume_is_flat_stopped_scoped_and_single_use(tmp_path,monkeypatch):
    rt,ev,st,put=fixture(tmp_path,monkeypatch);now=datetime.now(timezone.utc)
    (tmp_path/'main.py').write_text('SYNTHETIC_ONLY')
    (rt/'config').mkdir();put(rt/'config/paper_observation_five.local.json',{})
    plan=dict(new_config_sha256='NEW',new_core_sha256_lf='CORE',new_main_sha256_lf=release.lfsha(tmp_path/'main.py'))
    bundle=tmp_path/'bundle';bundle.mkdir();put(bundle/'release_plan.local.json',plan)
    c=dict(strategy_name='S0_SHORT_PAPER_CORE_V2',core_policy_sha256_lf='CORE',config_sha256='NEW',universe=['DCE.M'])
    monkeypatch.setattr(release,'load_paper_config',lambda *a,**k:c)
    monkeypatch.setattr(release,'facts_fresh',lambda *a:None)
    s=release.read(ev/'status.json');s.update(config_sha256='NEW',ready_products=5,products={'DCE.M':dict(ready=True,frozen=False,fault=None)},notifications=dict(configured=True,worker_alive=True));put(ev/'status.json',s)
    e=release.read(st/'execution.json');e['config_sha256']='NEW';put(st/'execution.json',e)
    h=release.read(ev/'gm_platform_host.local.json');h['config_sha256']='NEW';put(ev/'gm_platform_host.local.json',h)
    put(ev/'trend_reminder_health.local.json',dict(pid=77,at=now.isoformat()))
    with (ev/'runtime_events.jsonl').open('a',encoding='utf8') as f:
        f.write(json.dumps(dict(at=(now-timedelta(seconds=1)).isoformat(),kind='TREND_NOTIFIER_STARTED',pid=77))+'\n')
        f.write(json.dumps(dict(at=now.isoformat(),kind='GM_PLATFORM_HOST_STOPPED',flat_confirmed=True))+'\n')
    release.resume(tmp_path,bundle)
    assert release.read(st/'execution.json')['pause_new'] is False
    with pytest.raises(ValueError,match='consumed'):release.resume(tmp_path,bundle)


@pytest.mark.parametrize('case',['valid_stopped_child','stale_before_stop','future_health','older_host','foreign_pid','not_flat'])
def test_owned_notifier_is_checked_at_deliberate_stop_not_after_it(tmp_path,case):
    now=datetime.now(timezone.utc);stopped=now-timedelta(minutes=3)
    h=dict(pid=77,at=(stopped-timedelta(seconds=8)).isoformat())
    start=dict(pid=77,at=(stopped-timedelta(minutes=10)).isoformat())
    stop=dict(at=stopped.isoformat(),flat_confirmed=True)
    if case=='stale_before_stop':h['at']=(stopped-timedelta(seconds=31)).isoformat()
    if case=='future_health':h['at']=(stopped+timedelta(seconds=1)).isoformat()
    if case=='older_host':start['at']=(stopped-timedelta(seconds=2)).isoformat()
    if case=='foreign_pid':h['pid']=88
    if case=='not_flat':stop['flat_confirmed']=False
    if case=='valid_stopped_child':release.notifier_health_at_stop(h,start,stop)
    else:
        with pytest.raises(ValueError,match='healthy at reviewed shutdown'):release.notifier_health_at_stop(h,start,stop)


@pytest.mark.parametrize('case',['valid','before_stop','expired','future','foreign_notifier','stale_notifier'])
def test_post_stop_flat_proof_does_not_conflict_with_owned_notifier_health(case):
    now=datetime.now(timezone.utc);stopped=now-timedelta(seconds=5)
    h=dict(pid=77,at=(stopped-timedelta(seconds=2)).isoformat())
    start=dict(pid=77,at=(stopped-timedelta(minutes=2)).isoformat())
    stop=dict(at=stopped.isoformat(),flat_confirmed=False)
    proof=now.isoformat()
    if case=='before_stop':proof=(stopped-timedelta(seconds=1)).isoformat()
    if case=='expired':stop['at']=(now-timedelta(seconds=40)).isoformat();proof=(now-timedelta(seconds=31)).isoformat()
    if case=='future':proof=(now+timedelta(seconds=1)).isoformat()
    if case=='foreign_notifier':h['pid']=88
    if case=='stale_notifier':h['at']=(stopped-timedelta(seconds=31)).isoformat()
    if case=='valid':release.notifier_health_at_stop(h,start,stop,flat_proof_at=proof)
    else:
        with pytest.raises(ValueError):release.notifier_health_at_stop(h,start,stop,flat_proof_at=proof)


@pytest.mark.parametrize('case',['valid','expired','future','wrong_stop','wrong_host','wrong_main','wrong_config','risk_halt','different_pause','already_enabled','reused_token','reused_stop'])
def test_new_operator_decision_cannot_reuse_or_expand_old_release_authority(case):
    now=datetime.now(timezone.utc);plan=dict(new_config_sha256='PROFILE',new_main_sha256_lf='MAIN')
    host=dict(pid=123);stop=dict(at=(now-timedelta(minutes=2)).isoformat())
    e=dict(pause_new=True,halt_reason='',pause_metadata=dict(reason='GM_HOST_SHUTDOWN'),
        core_v2_resume_authorizations={'PROFILE':{'at':'OLD_DECISION'}})
    a=dict(token='SYNTHETIC_TOKEN',basis='NEW_EXPLICIT_USER_DECISION',issued_at=(now-timedelta(seconds=1)).isoformat(),
        expires_at=(now+timedelta(minutes=5)).isoformat(),stop_at=stop['at'],host_pid=123,main_sha256_lf='MAIN',config_sha256='PROFILE')
    if case=='expired':a['expires_at']=(now-timedelta(seconds=1)).isoformat()
    if case=='future':a['issued_at']=(now+timedelta(seconds=1)).isoformat()
    if case=='wrong_stop':a['stop_at']='OTHER_STOP'
    if case=='wrong_host':a['host_pid']=456
    if case=='wrong_main':a['main_sha256_lf']='OTHER_MAIN'
    if case=='wrong_config':a['config_sha256']='OTHER_PROFILE'
    if case=='risk_halt':e['halt_reason']='DAILY_LOSS_LIMIT'
    if case=='different_pause':e['pause_metadata']['reason']='GM1201'
    if case=='already_enabled':e['pause_new']=False
    if case in ('reused_token','reused_stop'):
        release.record_resume_authorization(e,plan,host,stop,a,now)
        if case=='reused_stop':a['token']='DIFFERENT_TOKEN'
        else:
            stop['at']=(now-timedelta(minutes=1)).isoformat();a['stop_at']=stop['at']
    if case=='valid':
        release.record_resume_authorization(e,plan,host,stop,a,now)
        assert e['core_v2_resume_authorizations']['PROFILE']=={'at':'OLD_DECISION'}
        assert len(e['operator_resume_authorizations'])==1 and e['pause_new'] is True
    else:
        with pytest.raises(ValueError):release.record_resume_authorization(e,plan,host,stop,a,now)
