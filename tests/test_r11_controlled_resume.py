from datetime import datetime,timezone,timedelta
import json
from unittest.mock import Mock
import pytest
from tools import paper_core_v2_release as base,paper_night_r11_release as release
from futures_trend_quant.live.paper_symbol_health import POLICY
from futures_trend_quant.live.paper_tq import POLICY as TQ
from test_paper_v2_release import fixture
from test_symbol_market_isolation import PAIRS


@pytest.mark.parametrize('case',['healthy_gm_tq_down','healthy_tq_gm_down','gm_healthy_tq_facts_behind','trade_down','no_market_proof','no_healthy','stale_facts','active','halt','no_auth'])
def test_real_controlled_resume_supports_ten_product_isolation_but_keeps_shared_and_single_use_gates(tmp_path,monkeypatch,case):
    rt,ev,st,put=fixture(tmp_path,monkeypatch);now=datetime.now(timezone.utc)
    (tmp_path/'main.py').write_text('SYNTHETIC_ONLY');(rt/'config').mkdir();put(rt/'config/paper_observation_five.local.json',{})
    c=dict(strategy_name='S0_SHORT_PAPER_CORE_V2',core_policy_sha256_lf='CORE',config_sha256='NEW',universe=[cs for cs,s in PAIRS],
        symbol_market_policy=POLICY,external_market_policy=TQ,notification_policy_version='ACTIONABLE_CARDS_V4_20261106')
    monkeypatch.setattr(base,'load_paper_config',lambda *a,**k:c)
    def check_facts(rt,ev,status):
        if case=='stale_facts' or (case=='gm_healthy_tq_facts_behind' and next(iter(status['products'])) in {cs for cs,s in PAIRS[5:]}):raise ValueError('STALE_FACTS')
    facts=Mock(side_effect=check_facts);monkeypatch.setattr(base,'facts_fresh',facts)
    healthy={cs for cs,s in (PAIRS[5:] if case=='healthy_tq_gm_down' else PAIRS[:5])}
    if case=='no_healthy':healthy=set()
    declared={cs for cs,s in PAIRS} if case=='gm_healthy_tq_facts_behind' else healthy
    s=base.read(ev/'status.json');s.update(config_sha256='NEW',ready_products=len(healthy),
        products={cs:dict(symbol=symbol,ready=cs in declared,frozen=cs not in declared,fault=None if cs in declared else 'SOURCE_DOWN') for cs,symbol in PAIRS},
        symbol_entry_readiness={symbol:dict(market_proof=dict(symbol=symbol,ready=cs in declared and case!='no_market_proof')) for cs,symbol in PAIRS},
        notifications=dict(configured=True,worker_alive=True),platform_connections=dict(market=dict(status='DISCONNECTED' if case=='healthy_tq_gm_down' else 'CONNECTED',run_id='RUN'),host_trade=dict(status='DISCONNECTED' if case=='trade_down' else 'CONNECTED',run_id='RUN')))
    if case=='active':s['broker']['active_intents']=1
    put(ev/'status.json',s);put(ev/'run_manifest.json',dict(run_id='RUN'))
    e=base.read(st/'execution.json');e.update(config_sha256='NEW',pause_metadata=dict(reason='GM_HOST_SHUTDOWN'))
    if case=='halt':e['halt_reason']='REAL_RISK_HALT'
    put(st/'execution.json',e);host=base.read(ev/'gm_platform_host.local.json');host['config_sha256']='NEW';put(ev/'gm_platform_host.local.json',host)
    put(ev/'trend_reminder_health.local.json',dict(pid=77,at=now.isoformat()))
    stop=dict(at=now.isoformat(),kind='GM_PLATFORM_HOST_STOPPED',flat_confirmed=True)
    with (ev/'runtime_events.jsonl').open('a',encoding='utf8') as f:
        f.write(json.dumps(dict(at=(now-timedelta(seconds=1)).isoformat(),kind='TREND_NOTIFIER_STARTED',pid=77))+'\n');f.write(json.dumps(stop)+'\n')
    plan=dict(new_config_sha256='NEW',new_core_sha256_lf='CORE',new_main_sha256_lf=base.lfsha(tmp_path/'main.py'));bundle=tmp_path/'bundle';bundle.mkdir();put(bundle/'release_plan.local.json',plan)
    auth=dict(token='SYNTHETIC_NEW_EXPLICIT_AUTH',basis='TEST_NEW_USER_DECISION',issued_at=(now-timedelta(seconds=1)).isoformat(),expires_at=(now+timedelta(minutes=5)).isoformat(),stop_at=stop['at'],host_pid=host['pid'],main_sha256_lf=plan['new_main_sha256_lf'],config_sha256='NEW')
    if case in ('healthy_gm_tq_down','healthy_tq_gm_down','gm_healthy_tq_facts_behind'):
        base.resume(tmp_path,bundle,authorization=auth)
        after=base.read(st/'execution.json');assert not after['pause_new'] and not after['halt_reason']
        assert {cs for call in facts.call_args_list for cs in call.args[2]['products']}==declared
        receipt=next(bundle.glob('controlled_resume_*.local.json'));assert base.read(receipt)['facts_qualified']==5
        after['pause_new']=True;put(st/'execution.json',after)
        with pytest.raises(ValueError,match='consumed'):base.resume(tmp_path,bundle,authorization=auth)
    else:
        with pytest.raises(ValueError):base.resume(tmp_path,bundle,authorization=None if case=='no_auth' else auth)
        assert base.read(st/'execution.json')['pause_new']
        assert not base.read(st/'execution.json').get('operator_resume_authorizations')


def test_r11_installer_recovery_entry_requires_new_explicit_authorization(tmp_path):
    with pytest.raises(ValueError,match='FRESH_EXPLICIT'):release.resume(tmp_path,tmp_path/'package')
