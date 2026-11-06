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


def test_controlled_resume_is_flat_stopped_scoped_and_single_use(tmp_path,monkeypatch):
    rt,ev,st,put=fixture(tmp_path,monkeypatch);now=datetime.now(timezone.utc)
    (tmp_path/'main.py').write_text('SYNTHETIC_ONLY')
    (rt/'config').mkdir();put(rt/'config/paper_observation_five.local.json',{})
    plan=dict(new_config_sha256='NEW',new_core_sha256_lf='CORE',new_main_sha256_lf=release.lfsha(tmp_path/'main.py'))
    bundle=tmp_path/'bundle';bundle.mkdir();put(bundle/'release_plan.local.json',plan)
    c=dict(strategy_name='S0_SHORT_PAPER_CORE_V2',core_policy_sha256_lf='CORE',config_sha256='NEW',universe=['DCE.M'])
    monkeypatch.setattr(release,'load_paper_config',lambda *a,**k:c)
    s=release.read(ev/'status.json');s.update(config_sha256='NEW',ready_products=5,products={'DCE.M':dict(ready=True,frozen=False,fault=None)},notifications=dict(configured=True,worker_alive=True));put(ev/'status.json',s)
    e=release.read(st/'execution.json');e['config_sha256']='NEW';put(st/'execution.json',e)
    h=release.read(ev/'gm_platform_host.local.json');h['config_sha256']='NEW';put(ev/'gm_platform_host.local.json',h)
    put(ev/'trend_reminder_health.local.json',dict(pid=77,at=now.isoformat()))
    with (ev/'runtime_events.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(dict(at=now.isoformat(),kind='TREND_NOTIFIER_STARTED',pid=77))+'\n')
    release.resume(tmp_path,bundle)
    assert release.read(st/'execution.json')['pause_new'] is False
    with pytest.raises(ValueError,match='consumed'):release.resume(tmp_path,bundle)
