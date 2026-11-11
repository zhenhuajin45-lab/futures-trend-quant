from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from collections import Counter
import json
import pytest
import pandas as pd
from futures_trend_quant.live.paper_symbol_health import POLICY,SERVICE_POLICY,symbol_status,display_status
from futures_trend_quant.live.paper_service_health import require_service,ServiceProofError
from futures_trend_quant.live.paper_tq import POLICY as TQ,append,atomic
from futures_trend_quant.live.paper_products import TEN_POOL
from test_paper_execution import NOW,signal,quote,snapshot,engine
from test_paper_service_health import runner
from test_paper_runtime_health_v6 import setup_health

PAIRS=list(zip(TEN_POOL,['DCE.m2701','SHFE.au2612','CZCE.OI701','DCE.y2701','DCE.p2701','CZCE.MA701','DCE.eg2701','DCE.jd2612','CZCE.CJ701','SHFE.ag2702']))


def fixture(tmp_path,monkeypatch):
    r,t=runner(tmp_path,at=NOW);r.c.update(root=str(tmp_path),universe=list(TEN_POOL),symbol_market_policy=POLICY,external_market_policy=TQ)
    setup_health(r.c);r.run_id='CURRENT'
    alive=SimpleNamespace(thread=SimpleNamespace(is_alive=lambda:True))
    r.products={cs:dict(metadata={'symbol':s},ready=True,frozen=False,fault=None,market=alive,core=alive) for cs,s in PAIRS}
    r.records={s:dict(trade_windows=[(NOW.timestamp()-100,NOW.timestamp()+100)]) for cs,s in PAIRS}
    for cs,s in PAIRS:
        if cs in TEN_POOL[5:]:r.products[cs].update(ready=False,frozen=True,fault='TQ_DATA_SIDECAR_EXITED')
    r.tq_feed=SimpleNamespace(is_alive=lambda:False)
    atomic(r.evidence/'run_manifest.json',dict(run_id='CURRENT',config_sha256=r.c['config_sha256'],service_policy_version=SERVICE_POLICY))
    import futures_trend_quant.live.paper_symbol_health as module
    monkeypatch.setattr(module,'datetime',SimpleNamespace(now=lambda *a:NOW.to_pydatetime()))
    return r,t


@pytest.mark.parametrize('cs,symbol',PAIRS[:5])
def test_all_five_healthy_gm_symbols_are_not_vetoed_by_dead_tq_worker(tmp_path,monkeypatch,cs,symbol):
    r,_=fixture(tmp_path,monkeypatch);r.service_health.publish(NOW,True)
    assert r.service_health.state['service_facts_ready'] and r.service_health.state['entry_gate_ready']
    assert require_service(r.c,NOW,symbol=symbol)['symbol_market_proof']['ready']
    with pytest.raises(ServiceProofError,match='SYMBOL_MARKET_NOT_READY'):require_service(r.c,NOW,symbol=PAIRS[5][1])
    assert not r.engine.s['pause_new']


@pytest.mark.parametrize('bad',['cash','pause','halt','trade_connection','unknown_intent'])
def test_shared_account_faults_still_block_healthy_symbol(tmp_path,monkeypatch,bad):
    r,_=fixture(tmp_path,monkeypatch)
    if bad=='cash':r.engine.last_snapshot=str(NOW-pd.Timedelta(seconds=4))
    elif bad=='pause':r.engine.s['pause_new']=True
    elif bad=='halt':r.engine.s['halt_reason']='REAL_SHARED_RISK'
    elif bad=='trade_connection':r.platform_connections['host_trade']['status']='DISCONNECTED'
    else:r.engine.s['intents']['OTHER']=dict(status=10,cancel_outcome_unknown=True)
    r.service_health.publish(NOW,True)
    with pytest.raises(ServiceProofError):require_service(r.c,NOW,symbol=PAIRS[0][1])


def test_gm_market_failure_does_not_mutate_account_pause_or_block_healthy_tq(tmp_path,monkeypatch):
    r,_=fixture(tmp_path,monkeypatch);r.tq_feed=SimpleNamespace(is_alive=lambda:True)
    for cs,s in PAIRS[5:]:r.products[cs].update(ready=True,frozen=False,fault=None)
    proof=dict(policy=TQ,config_sha256=r.c['config_sha256'],run_id='CURRENT',status='RUNNING',updated_at=str(NOW),
        contracts=dict(PAIRS[5:]),main_mapping=dict(PAIRS[5:]),product_status={cs:dict(ready=True) for cs,s in PAIRS[5:]})
    append(r.evidence/'tq_feed/CURRENT/health.jsonl',proof)
    r.platform_connections['market']['status']='DISCONNECTED';r.service_health.fault('market','DISCONNECTED',NOW)
    assert not r.engine.s['pause_new'] and r.service_health.active is None
    require_service(r.c,NOW,symbol=PAIRS[5][1])
    with pytest.raises(ServiceProofError,match='SYMBOL_MARKET_NOT_READY'):require_service(r.c,NOW,symbol=PAIRS[0][1])
    proof['product_status'][PAIRS[5][0]]=dict(ready=False,reason='TQ_NONFINITE_QUOTE');append(r.evidence/'tq_feed/CURRENT/health.jsonl',proof)
    r.service_health.publish(NOW+pd.Timedelta(milliseconds=10),True)
    with pytest.raises(ServiceProofError):require_service(r.c,NOW+pd.Timedelta(milliseconds=10),symbol=PAIRS[5][1])
    require_service(r.c,NOW+pd.Timedelta(milliseconds=10),symbol=PAIRS[6][1])


@pytest.mark.parametrize('direction',['bull','bear'])
def test_healthy_gm_candidate_crosses_real_engine_wal_and_final_native_with_tq_down(tmp_path,monkeypatch,direction):
    from test_paper_entry_v5 import v5_signal
    from test_paper_bridge import invoke,req
    from futures_trend_quant.live.paper_config import digest
    r,t=fixture(tmp_path/'engine',monkeypatch);e=r.engine
    e.c.update(strategy_name='S0_SHORT_PAPER_CORE_V2',entry_policy_version='PAPER_ENTRY_V5',execution_policy_version='PAPER_EXEC_V5')
    s=v5_signal(NOW)
    if direction=='bear':
        from test_paper_r7_async_five import short_signal
        s=short_signal('DCE.m2701','DCE.M')
    requests=[];created={}
    profile=dict(schema_version=2,universe=list(TEN_POOL),pool_policy_version='TEN_MIXED_TQ_V1_20261111',market_registry='market.json',max_positions=2,
        strategy_name='S0_SHORT_PAPER_CORE_V2',entry_policy_version='PAPER_ENTRY_V5',execution_policy_version='PAPER_EXEC_V5',
        clock_policy_version='PAPER_CLOCK_HEALTH_V1_20261109',quote_policy_version='GM_HOST_TICK_PRIMARY_V1_20261109',external_market_policy=TQ,
        symbol_market_policy=POLICY,state_dir=r.c['state_dir'],evidence_dir=r.c['evidence_dir'])
    def before(root,config):
        r.c['config_sha256']=digest(config);e.s['config_sha256']=digest(config);e.save();setup_health(r.c)
        atomic(r.evidence/'run_manifest.json',dict(run_id='CURRENT',config_sha256=r.c['config_sha256'],service_policy_version=SERVICE_POLICY))
        e.quote('DCE.m2701',dict(quote(),source='GM_HOST_TICK_CALLBACK'),NOW);r.service_health.publish(NOW,True)
        assert e.offer(s,NOW)[1]=='CANDIDATE_WAITING';e.drain_candidates(NOW);created.update(t.jobs[-1]);assert created['op']=='submit'
        r.transport.writer=SimpleNamespace(health=lambda:dict(state='PENDING',op='submit',rpc_id=created['id']))
        r.service_health.publish(NOW,True)
        assert r.service_health.state['pending_entry_request']['symbol']=='DCE.m2701'
        atomic(r.evidence/'latest_quotes/DCE.m2701.json',dict(config_sha256=digest(config),host_run_id='CURRENT',symbol='DCE.m2701',received_at=str(NOW),quote=dict(e.quotes['DCE.m2701'],source='GM_HOST_TICK_CALLBACK')))
        requests.append(req('submit',created['id'],arguments=created['args']))
    class Clock:
        fromisoformat=staticmethod(__import__('datetime').datetime.fromisoformat)
        @staticmethod
        def now(tz):return NOW.to_pydatetime()
    replies,calls=invoke(tmp_path,monkeypatch,requests,profile_extra=profile,before_serve=before,clock=Clock,
        contracts={'DCE.m2701':dict(csymbol='DCE.M',tick=1,multiplier=10,margin_ratio=.07,trade_windows=[(NOW.timestamp()-60,NOW.timestamp()+60)],entry_windows=[(NOW.timestamp()-60,NOW.timestamp()+60)])})
    assert replies[0]['ok'] and replies[0]['native_submit_invoked'],replies[0]
    assert len([x for op,x in calls if op=='submit'])==1


def test_unavailable_product_is_absent_from_real_signal_summary_healthy_product_remains(tmp_path):
    from test_paper_trend_notify import fixture as make,put,NOW as at
    from tools.paper_trend_notify import rows,deliver
    root=make(tmp_path);put(root/'config/paper_observation_five.local.json',dict(symbol_market_policy=POLICY))
    status=json.loads((root/'evidence/paper_observation_five/status.json').read_text());status['config_sha256']='C'
    for p in status['products'].values():p.update(ready=False,frozen=True,fault='NO_FEED')
    p=status['products']['DCE.M'];p.update(ready=True,frozen=False,fault=None)
    put(root/'state/paper_observation_five/market_registry.local.json',dict(contracts={p['symbol']:dict(csymbol='DCE.M',trade_windows=[(at.timestamp()-60,at.timestamp()+60)])}))
    put(root/'evidence/paper_observation_five/run_manifest.json',dict(run_id='R'))
    put(root/'evidence/paper_observation_five/latest_quotes'/(p['symbol']+'.json'),dict(symbol=p['symbol'],config_sha256='C',host_run_id='R',received_at=at.isoformat(),quote=dict(last=100,created_at=at.isoformat(),source='GM_HOST_TICK_CALLBACK')))
    put(root/'evidence/paper_observation_five/status.json',status)
    assert [x['csymbol'] for x in rows(root,at)[1]]==['DCE.M']
    key=deliver(tmp_path,'ISOLATED','多品种趋势',at);text=(root/'evidence/paper_observation_five/notifications'/(key+'.json')).read_text(encoding='utf8')
    assert '豆粕' in text and '鸡蛋' not in text and '红枣' not in text


@pytest.mark.parametrize('bad_index',range(5))
def test_actual_tq_publish_loop_isolates_one_nonfinite_quote_without_losing_other_four(tmp_path,monkeypatch,bad_index):
    import sys,time
    from types import ModuleType
    from futures_trend_quant.live import paper_tq_feed as feed,paper_tq as tq
    from tools.supplemental_tq import credentials
    root=tmp_path;profile=root/'config/paper.json';profile.parent.mkdir();profile.write_text(json.dumps(dict(symbol_market_policy=POLICY,evidence_dir='ev',tq_feed={'auth_file':'SYNTHETIC'})))
    products={}
    for cs,symbol in PAIRS[5:]:
        p=root/(cs+'.json');p.write_text(json.dumps(dict(spec=dict(price_tick=1,multiplier=10))))
        products[cs]=dict(symbol=symbol,metadata=p.name)
    fake=ModuleType('tqsdk');fake.__version__='3.10.4';fake.TqSim=lambda:None;fake.TqAuth=lambda *a:None
    symbols=[s for cs,s in PAIRS[5:]]
    class Api:
        def __init__(self,**k):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get_quote(self,s):
            actual=next((v for v in symbols if v.split('.')[0] in s and ''.join(c for c in v.split('.')[1] if c.isalpha()) in s),symbols[0]) if s.startswith('KQ.') else s
            return SimpleNamespace(datetime=NOW.strftime('%Y-%m-%d %H:%M:%S.%f'),price_tick=1,volume_multiple=10,last_price=100,
                bid_price1=99,ask_price1=float('nan') if s==symbols[bad_index] else 101,bid_volume1=5,ask_volume1=5,underlying_symbol=actual)
        def get_kline_serial(self,*a,**k):return pd.DataFrame([dict(datetime=NOW.value-120_000_000_000+i*60_000_000_000,open=100,high=101,low=99,close=100,volume=5,close_oi=200) for i in range(2)])
        def is_serial_ready(self,*a):return True
        def wait_update(self,**k):time.sleep(.02)
    fake.TqApi=Api;monkeypatch.setitem(sys.modules,'tqsdk',fake)
    monkeypatch.setattr(tq,'validate_config',lambda *a:None);monkeypatch.setattr(feed,'load_manifest',lambda *a:dict(products=products));monkeypatch.setattr(credentials,'authentication',lambda:('SYNTHETIC','SYNTHETIC'))
    spool=root/'ev/tq_feed/GEN';feed.publish(profile,'GEN',spool,seconds=.06)
    health=[json.loads(x) for x in (spool/'health.jsonl').read_text().splitlines()]
    running=[x for x in health if x['status']=='RUNNING'];assert running
    assert not running[-1]['product_status'][PAIRS[5+bad_index][0]]['ready']
    assert running[-1]['product_status'][PAIRS[5+bad_index][0]]['reason']=='TQ_NONFINITE_QUOTE'
    assert all(running[-1]['product_status'][cs]['ready'] and (spool/(cs.replace('.','_')+'.quotes.jsonl')).exists() for cs,s in PAIRS[5:] if s!=symbols[bad_index])
    assert not any(x['status']=='ERROR' for x in health)


def test_event_identity_accepts_equivalent_offset_and_blocks_old_or_unknown_checkpoint():
    from futures_trend_quant.live.paper_symbol_health import current_m15_event
    s=dict(symbol_market_policy=POLICY,products={'DCE.M':dict(core={'last_closed_m15':'2026-11-11 21:15:00+08:00'})})
    assert current_m15_event(s,'DCE.M','2026-11-11T13:15:00+00:00')
    assert not current_m15_event(s,'DCE.M','2026-11-11T21:00:00+08:00')
    assert not current_m15_event(s,'UNKNOWN',None)


def test_real_coordinator_report_keeps_shared_and_symbol_readiness_separate(tmp_path,monkeypatch):
    from futures_trend_quant.live.paper_five import FiveRunner
    r,_=fixture(tmp_path,monkeypatch);alive=SimpleNamespace(is_alive=lambda:True)
    for cs,s in PAIRS:
        r.calendar.register(s,r.calendar.for_symbol('DCE.m2701'))
        r.products[cs].update(calendar=r.calendar.for_symbol(s),coverage=dict(qualified=True,expected_m15=1200,missing_m1=0),counts=Counter(),received={})
        r.products[cs]['core']=SimpleNamespace(thread=alive,progress={})
    r.readonly=False;r.verified=False;r.started=r.observation_started=NOW;r.max_gap=0;r.notifications=None;r.quote_worker=alive
    r.supervise_trend_notifier=Mock();r.supervise_notification_sender=Mock();r.tq_feed.spool=r.evidence/'tq_feed/CURRENT'
    r.service_health.publish(NOW,True);r.report=lambda now:FiveRunner.report(r,now)
    result=r.report(NOW)
    assert result['ready_products']==5
    assert result['entry_readiness']['account_enabled']
    assert result['entry_readiness']['state']=='ACCOUNT_READY_CHECK_EACH_SYMBOL'
    assert result['symbol_entry_readiness']['DCE.m2701']['market_proof']['ready']
    assert not result['symbol_entry_readiness']['CZCE.MA701']['ready_for_candidate_checks']


def test_real_preopen_inspector_reports_product_outage_without_whole_pool_trade_veto(tmp_path):
    import importlib.util,hashlib
    from datetime import datetime,timezone
    from test_paper_trend_notify import put
    path=Path(__file__).resolve().parents[1]/'ops/night_inspect.py';spec=importlib.util.spec_from_file_location('r10_ops',path);ops=importlib.util.module_from_spec(spec);spec.loader.exec_module(ops)
    (tmp_path/'main.py').write_text('# SYNTHETIC_NO_EXECUTION')
    ev=tmp_path/'_strategy_runtime/evidence/paper_observation_five';now=datetime.now(timezone.utc).isoformat()
    put(ev/'gm_platform_host.local.json',dict(pid=12345,entry_sha256_lf=hashlib.sha256((tmp_path/'main.py').read_bytes()).hexdigest()))
    put(ev/'status.json',dict(at=now,ready_products=5,universe=list(TEN_POOL),symbol_market_policy=POLICY,phase='PARTIAL_PRODUCT_READINESS',
        broker=dict(status='RECONCILED',pause_new=False,halt_reason='',positions=[],active_intents=0),products={cs:dict(market_alive=i<5,core_alive=True,fault=None if i<5 else 'TQ_WORKER_NOT_ALIVE') for i,(cs,s) in enumerate(PAIRS)},
        quote_worker_alive=True,notifications=dict(worker_alive=True,health_fresh=True,outbox_statuses={}),symbol_entry_readiness={'DCE.m2701':dict(ready_for_candidate_checks=True),'CZCE.MA701':dict(ready_for_candidate_checks=False)}))
    result=ops.inspect(tmp_path)
    assert result['readiness_scope']=='SHARED_ACCOUNT_AND_SEPARATE_SYMBOLS'
    assert 'PRODUCTS_NOT_ALL_READY' not in result['issues']
    assert result['symbol_readiness']['DCE.m2701']['ready_for_candidate_checks']
    assert any('CZCE.MA' in x for x in result['issues'])


def test_pa_local_card_suppressed_for_unavailable_symbol_without_affecting_other_symbol(tmp_path,monkeypatch):
    from futures_trend_quant.observation.price_action.worker import write_candidate_card
    from futures_trend_quant.observation.price_action import cards
    from futures_trend_quant.live import paper_symbol_health
    monkeypatch.setattr(paper_symbol_health,'display_status',lambda root,cs,now:dict(ready=cs=='DCE.M'))
    monkeypatch.setattr(cards,'card',lambda result:dict(symbol=result['csymbol'],mode='SHADOW'))
    ev=tmp_path/'pa';bad=dict(csymbol='CZCE.MA',candidate_evidence_id='BAD');good=dict(csymbol='DCE.M',candidate_evidence_id='GOOD')
    assert not write_candidate_card(tmp_path,ev,'BAD',bad,NOW)
    assert write_candidate_card(tmp_path,ev,'GOOD',good,NOW)
    assert not (ev/'cards/BAD.local.json').exists()
    assert json.loads((ev/'cards/GOOD.local.json').read_text())['symbol']=='DCE.M'
    assert bad==dict(csymbol='CZCE.MA',candidate_evidence_id='BAD')


def test_quote_handoff_rejects_only_bad_symbol_and_continues_next_healthy_quote(tmp_path,monkeypatch):
    from futures_trend_quant.live.paper_five import FiveRunner
    r,_=fixture(tmp_path,monkeypatch);r.notice=Mock();r.run_id='R'
    r.accept_quote=Mock(side_effect=[ValueError('BAD_QUOTE'),True])
    assert not FiveRunner.accept_feed_quote(r,PAIRS[0][1],{},NOW)
    assert FiveRunner.accept_feed_quote(r,PAIRS[1][1],{},NOW)
    assert r.products[PAIRS[0][0]]['fault']=='QUOTE_INPUT_REJECTED'
    assert r.products[PAIRS[1][0]]['ready'] and not r.products[PAIRS[1][0]]['frozen']
    assert not r.engine.s['pause_new']


def test_unknown_instrument_gm_subscription_failure_revokes_only_gm_proofs(tmp_path,monkeypatch):
    from futures_trend_quant.live import gm_paper_host as host
    monkeypatch.setattr(host,'datetime',SimpleNamespace(now=lambda *a:NOW.to_pydatetime()))
    r,_=fixture(tmp_path,monkeypatch);r.notice=Mock();r.m1_subscriptions={s for cs,s in PAIRS[:5]}
    host.on_error(SimpleNamespace(ftq_paper_runner=r),1202,'PRIVATE_VENDOR_TEXT_NOT_RECORDED')
    assert not r.tick_subscriptions and not r.m1_subscriptions
    assert not r.engine.s['pause_new'] and r.service_health.active is None
    assert r.service_health.state['service_facts_ready']
    assert all(not r.service_health.state['symbol_readiness'][s]['ready'] for cs,s in PAIRS[:5])
