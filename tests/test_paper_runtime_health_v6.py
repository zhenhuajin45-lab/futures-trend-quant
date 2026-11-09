"""V6 production-shaped clock, tick -> native-cache, and terminal regressions.

No SDK network, clock changes, or paper orders are made by these tests.
"""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import queue
import time
from collections import Counter
import pandas as pd
import pytest
from futures_trend_quant.live import paper_clock_health as ch
from futures_trend_quant.live import gm_paper_host as host
from futures_trend_quant.live.paper_five import FiveRunner
from futures_trend_quant.live.paper_quote_feed import POLICY,TICK_SOURCE,supersedes
from futures_trend_quant.live.paper_quote_cache import latest_quote
from test_paper_execution import engine,signal,quote,NOW
from tools.paper_trend_notify import trend_risk


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),encoding='utf8')


def setup_health(config,offset=.01,**changes):
    config['clock_policy_version']=ch.VERSION
    ev=Path(config['evidence_dir']);write(ev/'run_manifest.json',dict(run_id='CURRENT'))
    row=ch.collect(config['config_sha256'],'CURRENT',lambda source:dict(source=source,offset_seconds=offset,rtt_seconds=.03,stratum=2))
    row.update(changes);write(ev/'clock_health.local.json',row);return row


@pytest.mark.parametrize('offset,expected',[(.01,'HEALTHY'),(-.02,'HEALTHY'),(.5,'HEALTHY'),(1.79,'UNTRUSTED'),(-1.79,'UNTRUSTED')])
def test_clock_budget_preserves_existing_three_second_quote_contract(offset,expected):
    r=ch.collect('C','R',lambda s:dict(source=s,offset_seconds=offset,rtt_seconds=.01))
    assert r['status']==expected and r['clock_changed'] is False


def test_clock_quorum_failure_does_not_refresh_success_or_correct_os_clock(tmp_path):
    def missing(source):raise TimeoutError('SYNTHETIC')
    r=ch.collect('C','R',missing)
    assert r['status']=='UNTRUSTED' and len(r['errors'])==2 and r['measured_offset_seconds'] is None


def test_transient_packet_loss_can_only_reuse_original_bounded_clock_sample():
    good=ch.collect('C','R',lambda s:dict(source=s,offset_seconds=.01))
    failed=dict(good,status='UNTRUSTED',reason='CLOCK_SOURCE_QUORUM',sample_monotonic=good['sample_monotonic']+30,errors=['SYNTHETIC_TIMEOUT'])
    retained=ch.retain_bounded_sample(good,failed)
    assert retained['probe_degraded'] and retained['sample_monotonic']==good['sample_monotonic'] and retained['at']==good['at']
    expired=dict(failed,sample_monotonic=good['sample_monotonic']+91)
    assert ch.retain_bounded_sample(retained,expired)['status']=='UNTRUSTED'
    assert ch.retain_bounded_sample(good,dict(failed,reason='CLOCK_OFFSET'))['status']=='UNTRUSTED'
    assert ch.retain_bounded_sample(good,dict(failed,run_id='NEW'))['status']=='UNTRUSTED'


def test_disagreement_and_nonfinite_clock_samples_fail_closed():
    r=ch.collect('C','R',lambda s:dict(source=s,offset_seconds=0 if s==ch.SOURCES[0] else .2))
    assert r['reason']=='CLOCK_SOURCE_QUORUM'
    r=ch.collect('C','R',lambda s:dict(source=s,offset_seconds=float('nan')))
    assert r['status']=='UNTRUSTED'


@pytest.mark.parametrize('mutation',['stale','future_mono','other_run','other_profile','step','untrusted','missing_source'])
def test_clock_health_identity_age_and_clock_step_are_not_trusted(tmp_path,mutation):
    e,_=engine(tmp_path);r=setup_health(e.c)
    if mutation=='stale':r['sample_monotonic']-=ch.MAX_AGE+1
    elif mutation=='future_mono':r['sample_monotonic']+=10
    elif mutation=='other_run':r['run_id']='OLD'
    elif mutation=='other_profile':r['config_sha256']='OLD'
    elif mutation=='step':r['at']=(pd.Timestamp.now(tz='UTC')-pd.Timedelta(seconds=2)).isoformat()
    elif mutation=='untrusted':r['status']='UNTRUSTED'
    else:r['samples']=r['samples'][:1]
    write(Path(e.c['evidence_dir'])/'clock_health.local.json',r)
    assert ch.health(e.c)['reason']=='SYSTEM_CLOCK_UNTRUSTED'


def test_untrusted_clock_reaches_real_engine_open_and_prevents_writeahead_or_rpc(tmp_path):
    e,t=engine(tmp_path);setup_health(e.c,offset=1.79)
    assert e.open(signal(),NOW)==(False,'SYSTEM_CLOCK_UNTRUSTED')
    assert not t.jobs and not e.s['intents'] and e.s['pause_new']
    assert e.s['pause_metadata']['reason']=='SYSTEM_CLOCK_UNTRUSTED'
    # A later healthy sample is evidence, not a new resume authorization.
    setup_health(e.c)
    assert e.open(signal(),NOW)==(False,'STOP_NEW_OPEN') and not t.jobs


def test_healthy_clock_allows_the_original_real_open_pipeline(tmp_path):
    e,t=engine(tmp_path);setup_health(e.c)
    assert e.open(signal(),NOW)[0]
    assert t.jobs[-1]['op']=='submit' and t.jobs[-1]['args']['volume']==1
    assert json.loads(e.path.read_text())['used_signals']==['E1']


def test_paused_candidate_terminal_is_persisted_and_not_reissued_after_resume(tmp_path):
    e,t=engine(tmp_path);e.pause_entries('GM_HOST_SHUTDOWN',NOW,'GM_HOST')
    assert e.offer(signal(),NOW)[1]=='CANDIDATE_WAITING'
    e.drain_candidates(NOW)
    final=e.s['candidate_status']['E1'];assert final['status']=='BLOCKED' and final['reason']=='STOP_NEW_OPEN'
    e.s['pause_new']=False;e.save()  # Synthetic explicit operator action, not production recovery.
    assert e.offer(signal(),NOW+pd.Timedelta(seconds=2))[1]=='CANDIDATE_BLOCKED'
    assert not t.jobs
    events=[json.loads(x) for x in (Path(e.c['evidence_dir'])/'order_events.jsonl').read_text().splitlines()]
    terminal=next(x for x in events if x['kind']=='CANDIDATE_FINISHED')
    assert terminal['symbol']=='DCE.m2701' and terminal['path']=='D' and terminal['expires_at']


def runner(tmp_path):
    e,t=engine(tmp_path/'engine');r=FiveRunner.__new__(FiveRunner)
    r.c=dict(e.c,root=str(tmp_path),quote_policy_version=POLICY);r.engine=e;r.evidence=Path(e.c['evidence_dir'])
    r.records={'DCE.m2701':dict(tick=1)};r.products={};r.host_ticks=queue.Queue();r.quote_events=queue.Queue()
    r.accepted_quotes={};r.quote_feed_counts=Counter();r.quote_feed_last={};r.event=Mock();return r,t


def tick(now=NOW,symbol='DCE.m2701',price=101):
    return dict(symbol=symbol,created_at=str(now),price=price,quotes=[dict(bid_p=100,ask_p=101,bid_v=50,ask_v=50)])


def test_actual_host_tick_callback_to_coordinator_cache_and_original_engine_quote(tmp_path):
    r,t=runner(tmp_path);context=SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False)
    host.on_tick(context,tick());assert r.host_ticks.qsize()==1 and not t.jobs
    r.market_events(NOW)
    q=r.engine.quotes['DCE.m2701'];cache=json.loads((r.evidence/'latest_quotes/DCE.m2701.json').read_text())
    assert q['source']==cache['quote']['source']==TICK_SOURCE
    assert q['created_at']==str(NOW) and cache['config_sha256']=='SYNTHETIC'


def test_late_poll_cannot_overwrite_fresh_tick_in_engine_or_native_cache(tmp_path):
    r,t=runner(tmp_path);fresh=dict(quote(),source=TICK_SOURCE)
    assert r.accept_quote('DCE.m2701',fresh,NOW)
    poll=dict(quote(NOW-pd.Timedelta(seconds=2)),source='GM_BULK_CURRENT_POLL')
    assert not r.accept_quote('DCE.m2701',poll,NOW+pd.Timedelta(seconds=1))
    assert not r.accept_quote('DCE.m2701',dict(fresh,source='GM_BULK_CURRENT_POLL'),NOW)
    assert r.engine.quotes['DCE.m2701']['source']==TICK_SOURCE
    assert json.loads((r.evidence/'latest_quotes/DCE.m2701.json').read_text())['quote']['created_at']==str(NOW)
    assert not t.jobs


def test_unknown_tick_contract_cannot_enter_quote_cache_or_order_path(tmp_path):
    r,t=runner(tmp_path);context=SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False)
    host.on_tick(context,tick(symbol='SHFE.au9999'))
    assert r.host_ticks.empty() and not t.jobs and not r.accepted_quotes


def native_fixture(tmp_path):
    e,t=engine(tmp_path);setup_health(e.c);e.c['quote_policy_version']=POLICY
    q=dict(quote(),source=TICK_SOURCE);write(Path(e.c['evidence_dir'])/'latest_quotes/DCE.m2701.json',dict(symbol='DCE.m2701',config_sha256='SYNTHETIC',received_at=str(NOW),quote=q))
    args=dict(symbol='DCE.m2701',position_effect=1,tick=1,price=101,side=1)
    return e,args


def test_native_final_guard_uses_same_clock_identity_and_tick_source(tmp_path):
    e,args=native_fixture(tmp_path)
    result=latest_quote(tmp_path,e.c,'SYNTHETIC',args,NOW.to_pydatetime())
    assert result['source']==TICK_SOURCE and result['limit_price_unchanged']==101
    setup_health(e.c,offset=1.79)
    with pytest.raises(ValueError,match='SYSTEM_CLOCK_UNTRUSTED'):latest_quote(tmp_path,e.c,'SYNTHETIC',args,NOW.to_pydatetime())


def test_clock_fault_does_not_add_a_new_exit_veto_or_weaken_three_second_quote_age(tmp_path):
    e,args=native_fixture(tmp_path);setup_health(e.c,offset=1.79);args['position_effect']=2
    assert latest_quote(tmp_path,e.c,'SYNTHETIC',args,NOW.to_pydatetime())
    with pytest.raises(ValueError,match='stale/future'):latest_quote(tmp_path,e.c,'SYNTHETIC',args,(NOW+pd.Timedelta(seconds=4)).to_pydatetime())


def test_tick_subscription_once_per_actual_contract_never_unsubscribes_other_feeds(tmp_path,monkeypatch):
    r,t=runner(tmp_path);r.tick_subscription_attempted=set();r.tick_subscriptions=set();r.cycle=Mock(return_value=False)
    sub=Mock();monkeypatch.setattr(host,'subscribe',sub)
    c=SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False,ftq_paper_in_cycle=False)
    host.on_timer(c);host.on_timer(c)
    sub.assert_called_once_with(symbols='DCE.m2701',frequency='tick',count=0,unsubscribe_previous=False)
    assert r.tick_subscriptions=={'DCE.m2701'} and not t.jobs


def test_subscription_error_persists_pause_without_retry_or_second_controller(tmp_path,monkeypatch):
    r,t=runner(tmp_path);r.tick_subscription_attempted=set();r.tick_subscriptions=set();r.cycle=Mock(return_value=False);r.notice=Mock();r.run_id='R'
    sub=Mock(side_effect=OSError('SYNTHETIC'));monkeypatch.setattr(host,'subscribe',sub)
    c=SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False,ftq_paper_in_cycle=False)
    host.on_timer(c);host.on_timer(c)
    assert sub.call_count==1 and r.engine.s['pause_new'] and not t.jobs


def test_periodic_card_joins_terminal_status_instead_of_repeating_waiting():
    item=dict(direction='bear',entry=dict(accepted=True,path='C'),evidence=dict(entry_policy_version='PAPER_ENTRY_V5',entry_permission=dict(new_entry_allowed=True)),execution_terminal=dict(status='BLOCKED',reason='STOP_NEW_OPEN'))
    text=trend_risk(item);assert '未下单' in text and '当前已停开' in text and '仍须' not in text
    item['execution_terminal']=dict(status='ABORTED_RESTART',reason='NO_HISTORICAL_RESEND')
    assert '未下单' in trend_risk(item) and 'NO_HISTORICAL_RESEND' in trend_risk(item)


@pytest.mark.parametrize('clock_bad,subscribed,allowed',[(True,True,False),(False,False,False),(False,True,True)])
def test_controlled_release_needs_live_clock_and_actual_subscription_evidence(tmp_path,monkeypatch,clock_bad,subscribed,allowed):
    from tools import paper_runtime_v6_release as release
    e,_=engine(tmp_path/'client'/'_strategy_runtime');setup_health(e.c,offset=1.79 if clock_bad else .01)
    client=tmp_path/'client';rt=client/'_strategy_runtime';bundle=tmp_path/'bundle'
    # Real health files are in the configured private evidence directory.
    e.c['quote_policy_version']=POLICY;e.c['root']=str(rt)
    write(rt/'config/paper_observation_five.local.json',{})
    write(bundle/'release_plan.local.json',dict(clock_policy_version=ch.VERSION,quote_policy_version=POLICY,runtime_health_version='V6'))
    symbols=['DCE.m2701','SHFE.au2612','CZCE.OI701','DCE.y2701','DCE.p2701']
    write(rt/'evidence/paper_observation_five/status.json',dict(products={str(n):dict(symbol=x) for n,x in enumerate(symbols)},quote_feed=dict(policy=POLICY,subscribed=symbols if subscribed else [])))
    monkeypatch.setattr(release.prior,'load_paper_config',lambda *a,**k:e.c)
    monkeypatch.setattr(release.prior,'flat_stopped',lambda *a:(rt,rt/'evidence/paper_observation_five',{},{}))
    measured=ch.collect('SYNTHETIC','CURRENT',lambda s:dict(source=s,offset_seconds=1.79 if clock_bad else .01))
    monkeypatch.setattr(release,'collect',lambda *a:measured)
    called=Mock(side_effect=lambda *args:write(bundle/'controlled_resume.local.json',dict(orders_sent=0)))
    monkeypatch.setattr(release.prior,'resume',called)
    if not allowed:
        with pytest.raises(ValueError):release.resume(client,bundle)
        called.assert_not_called()
    else:
        release.resume(client,bundle);called.assert_called_once()
        proof=json.loads((bundle/'controlled_resume.local.json').read_text())
        assert proof['clock_verified'] and proof['scope']=='THIS_REVIEWED_RELEASE_ONLY_NO_FUTURE_AUTO_RESUME'


@pytest.mark.parametrize('paused',[True,False])
def test_actual_status_report_does_not_confuse_warmed_workers_with_entry_permission(tmp_path,paused):
    from futures_trend_quant.live.paper_calendar import PortfolioCalendar
    from test_paper_execution import calendar
    e,t=engine(tmp_path/'engine');r=FiveRunner.__new__(FiveRunner)
    r.c=dict(e.c,universe=['DCE.M','SHFE.AU','CZCE.OI','DCE.Y','DCE.P'],quote_policy_version=POLICY)
    r.engine=e;r.evidence=Path(e.c['evidence_dir']);r.notifications=None;r.readonly=False;r.verified=False
    r.started=r.observation_started=NOW;r.max_gap=0;r.quote_worker=SimpleNamespace(is_alive=lambda:True)
    r.products={};r.records={};r.calendar=PortfolioCalendar()
    symbols=['DCE.m2701','SHFE.au2612','CZCE.OI701','DCE.y2701','DCE.p2701']
    alive=SimpleNamespace(is_alive=lambda:True)
    for cs,symbol in zip(r.c['universe'],symbols):
        cal=calendar();r.calendar.register(symbol,cal);r.records[symbol]={}
        r.products[cs]=dict(metadata=dict(symbol=symbol),calendar=cal,ready=True,coverage=dict(qualified=True,expected_m15=1200,missing_m1=0),frozen=False,fault=None,
            core=SimpleNamespace(progress={},thread=alive),market=SimpleNamespace(thread=alive),counts=Counter(),received={})
    r.tick_subscriptions=set(symbols);e.s['pause_new']=paused
    if paused:e.s['pause_metadata']=dict(reason='REVIEWED_HANDOFF',source='EXPLICIT_RELEASE')
    state=r.report(NOW)
    assert state['ready_products']==5
    assert state['entry_readiness']['account_enabled'] is (not paused)
    assert ('STOP_NEW_OPEN' in state['entry_readiness']['reasons']) is paused
    assert state['streaming_subscriptions']==5 and not t.jobs


def test_historical_terminal_cards_are_not_reopened_before_sender_heartbeat(tmp_path):
    from futures_trend_quant.live.paper_notify import pending_delivery_files
    old=tmp_path/('a'*64+'.json');old.write_text('UNREADABLE_ARCHIVED_PAYLOAD')
    pending=tmp_path/('b'*64+'.json');write(pending,dict(priority=3,created_at='T2'))
    urgent=tmp_path/('c'*64+'.json');write(urgent,dict(priority=1,created_at='T3'))
    beat=Mock()
    assert pending_delivery_files([old,pending,urgent],{old.stem},beat)==[urgent,pending]
    assert beat.call_count==3
