"""Actual host/engine/native gates; no network, process start or paper order API."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json,time
from collections import Counter
import queue
import pandas as pd
import pytest
from futures_trend_quant.live import gm_paper_host as host
from futures_trend_quant.live.paper_calendar import PortfolioCalendar
from futures_trend_quant.live.paper_service_health import ServiceHealth,POLICY,require_service
from futures_trend_quant.live.paper_transport import _SerialTransport
from futures_trend_quant.live.paper_notify import message
from futures_trend_quant.live.paper_quote_cache import latest_quote
from test_paper_execution import engine,snapshot,position,signal,NOW
from test_paper_runtime_health_v6 import setup_health,native_fixture

T=pd.Timestamp('2026-11-09T20:00:00+08:00')
SYMBOLS=['DCE.m2701','DCE.y2701','DCE.p2701','CZCE.OI701','SHFE.au2612']


def runner(tmp_path,at=T):
    e,transport=engine(tmp_path);e.apply_snapshot(snapshot(),at)
    cal=PortfolioCalendar()
    for s in SYMBOLS:cal.register(s,e.calendar)
    r=SimpleNamespace(engine=e,c=e.c,evidence=Path(e.c['evidence_dir']),run_id='SERVICE_TEST',calendar=cal,
        records={s:dict(trade_windows=[(pd.Timestamp('2026-11-09T21:00:00+08:00').timestamp(),pd.Timestamp('2026-11-09T23:00:00+08:00').timestamp())]) for s in SYMBOLS},
        products={s:dict(ready=True,frozen=False,fault=None) for s in SYMBOLS},tick_subscriptions=set(SYMBOLS),
        platform_connections={k:dict(status='CONNECTED') for k in ['market','host_trade']},event=Mock(),notice=Mock())
    r.reader_state=dict(state='IDLE',last_reply_ok=True,last_sent_at=str(at))
    r.transport=SimpleNamespace(reader=SimpleNamespace(health=lambda:r.reader_state))
    r.service_health=ServiceHealth(r)
    return r,transport


def recover(r,at):
    r.platform_connections['market']['status']='CONNECTED';r.reader_state=dict(state='IDLE',last_reply_ok=True,last_sent_at=str(at))
    r.engine.apply_snapshot(snapshot(),at);r.service_health.update(at)


def test_closed_flat_connection_episode_blocks_then_recovers_only_from_new_account_truth(tmp_path,monkeypatch):
    r,t=runner(tmp_path);monkeypatch.setattr(host,'datetime',SimpleNamespace(now=lambda *a:T.to_pydatetime()))
    c=SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False)
    host.on_error(c,1201,'PRIVATE_VENDOR_TEXT')
    assert not r.engine.s['pause_new'] and r.service_health.active['transient']
    assert not r.service_health.state['entry_gate_ready'] and not t.jobs
    r.service_health.update(T+pd.Timedelta(seconds=3))
    r.notice.assert_called_once();assert r.notice.call_args.kwargs['severity']=='warning'
    r.platform_connections['market']['status']='CONNECTED'
    r.service_health.update(T+pd.Timedelta(seconds=130))
    assert r.service_health.active is not None  # old account truth cannot recover
    recover(r,T+pd.Timedelta(seconds=131))
    assert r.service_health.active is None and r.service_health.state['entry_gate_ready']
    assert not r.engine.s['pause_new'] and len(r.notice.call_args_list)==2 and not t.jobs
    assert 'PRIVATE_VENDOR' not in str(r.event.call_args_list)+str(r.notice.call_args_list)


@pytest.mark.parametrize('case',['position','active','unknown_account','market_open','preopen','risk_halt','existing_pause','reader_exit','host_trade'])
def test_unknown_exposure_or_armed_session_never_uses_rest_quarantine(tmp_path,case):
    r,t=runner(tmp_path);at=T;lane='market';state='DISCONNECTED'
    if case=='position':r.engine.snapshot['positions']=[position()]
    if case=='active':r.engine.s['intents']['X']=dict(status=10,symbol=SYMBOLS[0],effect=1,direction='bull',filled=0,order_id='')
    if case=='unknown_account':r.engine.last_snapshot=None
    if case=='market_open':at=pd.Timestamp('2026-11-09T21:00:00+08:00');r.engine.last_snapshot=str(at)
    if case=='preopen':at=pd.Timestamp('2026-11-09T20:50:00+08:00');r.engine.last_snapshot=str(at)
    if case=='risk_halt':r.engine.s['halt_reason']='DAILY_LOSS_LIMIT'
    if case=='existing_pause':r.engine.s.update(pause_new=True,pause_metadata=dict(reason='USER_CONTROL',source='OPERATOR'))
    if case=='reader_exit':lane='reader';state='PROCESS_EXITED'
    if case=='host_trade':lane='host_trade'
    r.service_health.fault(lane,state,at)
    assert r.engine.s['pause_new'] and not r.service_health.active['transient']
    if case=='risk_halt':assert r.engine.s['halt_reason']=='DAILY_LOSS_LIMIT'
    if case=='existing_pause':assert r.engine.s['pause_metadata']['reason']=='USER_CONTROL'
    if case=='active':
        with pytest.raises(ValueError,match='submit outcome unknown'):recover(r,at+pd.Timedelta(seconds=1))
    else:recover(r,at+pd.Timedelta(seconds=1))
    assert r.engine.s['pause_new'] and not r.service_health.state['entry_gate_ready'] and not t.jobs


def test_unrecovered_rest_episode_latches_at_existing_ten_minute_preopen_deadline(tmp_path):
    r,t=runner(tmp_path);r.service_health.fault('market','DISCONNECTED',T)
    r.platform_connections['market']['status']='DISCONNECTED'
    at=pd.Timestamp('2026-11-09T20:50:00+08:00');r.service_health.update(at)
    assert r.engine.s['pause_new'] and not r.service_health.active['transient']
    recover(r,at+pd.Timedelta(seconds=1));assert r.engine.s['pause_new']


def test_slow_read_is_distinct_from_dead_transport_and_can_use_fresh_at_send_flat_proof(tmp_path):
    r,t=runner(tmp_path);r.engine.last_snapshot=str(T-pd.Timedelta(seconds=1))
    r.reader_state=dict(state='SLOW_PENDING',sent_at=str(T),request_age_seconds=4,op='snapshot')
    r.service_health.update(T+pd.Timedelta(seconds=4))
    assert r.service_health.active['transient'] and not r.engine.s['pause_new']
    assert r.service_health.active['lanes']['reader']=='SLOW_PENDING'
    recover(r,T+pd.Timedelta(seconds=32));assert r.service_health.active is None


def test_codes_and_reader_faults_share_one_episode_and_later_episode_gets_new_id(tmp_path):
    r,t=runner(tmp_path);r.platform_connections['market']['status']='CONNECT_FAILED'
    r.service_health.fault('market','DISCONNECTED',T,code=1201)
    for i in range(64):r.service_health.fault('market','CONNECT_FAILED',T+pd.Timedelta(seconds=i),code=1200)
    r.reader_state=dict(state='SLOW_PENDING',sent_at=str(T+pd.Timedelta(seconds=2)))
    r.service_health.update(T+pd.Timedelta(seconds=5));assert r.notice.call_count==1
    before=r.service_health.active['id'];recover(r,T+pd.Timedelta(seconds=131))
    assert r.notice.call_count==2 and r.service_health.completed['codes']=={'1201':1,'1200':64}
    r.service_health.fault('market','DISCONNECTED',T+pd.Timedelta(seconds=132));assert r.service_health.active['id']!=before


@pytest.mark.parametrize('mutation',['blocked','stale','old_run','old_profile','missing'])
def test_engine_and_native_final_service_proof_fail_closed_without_new_pause_or_submit(tmp_path,mutation):
    e,args=native_fixture(tmp_path);path=Path(e.c['evidence_dir'])/'service_health.local.json';row=json.loads(path.read_text())
    if mutation=='blocked':row['entry_gate_ready']=False
    if mutation=='stale':row['at']=str(NOW-pd.Timedelta(seconds=4))
    if mutation=='old_run':row['run_id']='OLD'
    if mutation=='old_profile':row['config_sha256']='OTHER'
    path.write_text(json.dumps(row),encoding='utf8')
    if mutation=='missing':path.unlink()
    assert e.open(signal(),NOW)==(False,'SERVICE_RECOVERY_PENDING')
    assert not e.transport.jobs and not e.s['intents'] and not e.s['pause_new']
    with pytest.raises(ValueError,match='SERVICE_RECOVERY_PENDING'):latest_quote(tmp_path,e.c,e.c['config_sha256'],args,NOW.to_pydatetime())
    args['position_effect']=2
    assert latest_quote(tmp_path,e.c,e.c['config_sha256'],args,NOW.to_pydatetime())['limit_price_unchanged']==101


def test_lane_health_reports_slow_pending_separately_and_keeps_writer_untouched():
    lane=_SerialTransport.__new__(_SerialTransport);lane.started=time.monotonic()-4;lane.closed=False
    lane.current=dict(op='snapshot',id='TEST',sent_at=str(T));lane.process=SimpleNamespace(poll=lambda:None);lane.last_outcome=None
    assert lane.health()['state']=='SLOW_PENDING'
    lane.process=SimpleNamespace(poll=lambda:1);assert lane.health()['state']=='PROCESS_EXITED'


def test_explicit_severity_controls_card_color_independently_from_title_words():
    assert message('平台错误',[],severity='critical')['card']['header']['template']=='red'
    assert message('休市期间服务波动',[],severity='warning')['card']['header']['template']=='orange'


@pytest.mark.parametrize('code',[1202,2003,1000])
def test_permissions_and_auth_errors_never_auto_recover_in_closed_flat_session(tmp_path,monkeypatch,code):
    r,t=runner(tmp_path);monkeypatch.setattr(host,'datetime',SimpleNamespace(now=lambda *a:T.to_pydatetime()))
    host.on_error(SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False),code,'PRIVATE_INFO')
    r.service_health.update(T)
    assert r.engine.s['pause_new'] and not r.service_health.active['transient']
    recover(r,T+pd.Timedelta(seconds=1));assert r.engine.s['pause_new'] and not t.jobs


def test_invalid_account_reply_latches_protection_even_during_rest_quarantine(tmp_path):
    r,t=runner(tmp_path);r.engine.c['clock_policy_version']='ENABLED_TEST'
    r.service_health.fault('market','DISCONNECTED',T)
    bad=snapshot();bad['cash']['account_id']='FOREIGN'
    r.engine.accept(dict(op='snapshot',id='READ_TEST'),dict(ok=True,data=bad),T)
    assert r.engine.s['pause_new'] and r.engine.s['pause_metadata']['reason']=='ACCOUNT_RECONCILIATION_FAILED'


def test_real_cycle_applies_fresh_account_and_publishes_service_proof_before_candidate_drain(tmp_path,monkeypatch):
    from futures_trend_quant.live import paper_five
    r,old=runner(tmp_path,at=NOW);setup_health(r.c);r.run_id='CURRENT'
    r.engine.last_snapshot=str(NOW-pd.Timedelta(seconds=4));r.engine.ready=False
    proof=Path(r.c['evidence_dir'])/'service_health.local.json';row=json.loads(proof.read_text());row['entry_gate_ready']=False;proof.write_text(json.dumps(row))
    r.service_health.state=dict(entry_gate_ready=False);r.service_health.last_write=NOW
    reply=(dict(op='snapshot',id='FRESH',sent_at=str(NOW)),dict(ok=True,data=snapshot(),local_received_at=str(NOW)))
    jobs=[];r.transport.drain=lambda:[reply];r.transport.busy=lambda:False;r.transport.hung=lambda:False
    r.transport.request=lambda op,args=None,rid=None:jobs.append(dict(op=op,args=args,id=rid))
    r.engine.transport=r.transport
    alive=SimpleNamespace(is_alive=lambda:True)
    for p in r.products.values():p.update(market=SimpleNamespace(thread=alive),core=SimpleNamespace(thread=alive,inbox=queue.Queue()),metadata=None,last_positions={})
    r.quote_worker=alive;r.last_loop=time.monotonic();r.max_gap=0;r.readonly=False
    r.market_events=Mock();r.core_events=Mock();r.restore_strategy_fills=Mock();r.pump_notifications=Mock();r.supervise_clock=Mock()
    r.last_problem=None;r.last_poll=time.monotonic();r.last_report=time.monotonic()
    monkeypatch.setattr(paper_five,'pd',SimpleNamespace(Timestamp=SimpleNamespace(now=lambda **kw:NOW)))
    assert r.engine.offer(signal(),NOW)[1]=='CANDIDATE_WAITING'
    paper_five.FiveRunner.cycle(r)
    assert json.loads(proof.read_text())['entry_gate_ready']
    assert len([j for j in jobs if j['op']=='submit'])==1 and r.engine.s['used_signals']==['E1']


def test_new_exposure_during_rest_quarantine_forces_review_not_automatic_recovery(tmp_path):
    r,t=runner(tmp_path);r.service_health.fault('market','DISCONNECTED',T)
    r.engine.snapshot['positions']=[position()]
    r.service_health.update(T+pd.Timedelta(seconds=1))
    episode=r.service_health.active or r.service_health.completed
    assert r.engine.s['pause_new'] and not episode['transient'] and not t.jobs
