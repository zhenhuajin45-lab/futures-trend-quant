import json
from pathlib import Path
import pandas as pd
import pytest
from test_paper_execution import calendar
from test_paper_service_health import runner,T,recover
from futures_trend_quant.live.paper_watermark import closure_status


@pytest.mark.parametrize('at,expected',[
    ('2026-11-05T11:30:01+08:00','2026-11-05T11:30:00+08:00'),
    ('2026-11-05T12:30:00+08:00','2026-11-05T11:30:00+08:00'),
    ('2026-11-05T13:30:00+08:00','2026-11-05T11:30:00+08:00'),
    ('2026-11-05T13:31:00+08:00','2026-11-05T13:31:00+08:00'),
])
def test_expected_watermark_does_not_promote_a_forming_bar_or_fill_lunch(at,expected):
    c=calendar();before=pd.Timestamp(expected)-pd.Timedelta(minutes=1)
    p=dict(last_closed_m1=before.isoformat(),last_closed_m15='2026-11-05T11:15:00+08:00')
    d=closure_status(c,p,at,'TQ_PREVIOUS_CONFIRMED_ONLY')
    assert pd.Timestamp(d['m1']['expected'])==pd.Timestamp(expected)
    assert d['m1']['status']=='PENDING_SOURCE_CLOSURE' and not d['forming_promoted'] and not d['affects_execution']
    assert p['last_closed_m1']==before.isoformat()


def test_watermark_unknown_future_and_day_only_night_are_explicit():
    from futures_trend_quant.live.paper_calendar import PaperCalendar
    c=PaperCalendar(['2026-11-10','2026-11-11','2026-11-12'],[dict(start='09:00',end='15:00')],'DCE.JD')
    d=closure_status(c,{},'2026-11-11T21:20:00+08:00','TQ')
    assert d['m1']['status']=='UNKNOWN' and d['m1']['expected'].endswith('15:00:00+08:00')
    assert closure_status(c,{'last_closed_m1':'2026-11-12T09:01:00+08:00'},'2026-11-11T21:20:00+08:00','TQ')['m1']['status']=='FUTURE_WATERMARK'


def test_service_facts_change_is_published_during_persistent_pause(tmp_path):
    r,_=runner(tmp_path);r.engine.s['pause_new']=True
    r.service_health.publish(T,True)
    assert r.service_health.state['service_facts_ready'] and not r.service_health.state['entry_gate_ready']
    r.tick_subscriptions=set();r.service_health.publish(T+pd.Timedelta(milliseconds=10))
    d=r.service_health.state
    assert not d['service_facts_ready'] and not d['entry_gate_ready'] and d['persistent_pause']
    assert d['facts_diagnostic']['market_sources']['failed_condition']=='GM_SUBSCRIPTIONS'


def test_service_recovery_card_records_unknown_first_delivery_without_resend(tmp_path):
    r,_=runner(tmp_path);calls=[]
    def notice(key,title,lines,**kwargs):
        outbox='NEW_'+str(len(calls));calls.append((key,title,lines));return outbox
    r.notice=notice;r.service_health.fault('market','DISCONNECTED',T)
    r.service_health.update(T+pd.Timedelta(seconds=3))
    folder=r.evidence/'notifications';folder.mkdir(exist_ok=True)
    (folder/'NEW_0.json').write_text(json.dumps({'status':'UNKNOWN_DELIVERY'}))
    recover(r,T+pd.Timedelta(seconds=4))
    assert len(calls)==2 and calls[0][0]!=calls[1][0]
    assert '此前故障通知送达结果未知' in '\n'.join(calls[1][2])
    assert r.service_health.completed['prior_notification_delivery'][0]['status']=='UNKNOWN_DELIVERY'
    assert json.loads((folder/'NEW_0.json').read_text())['status']=='UNKNOWN_DELIVERY'


def test_missing_snapshot_send_clock_rejects_without_diagnostic_exception(tmp_path):
    from test_paper_execution import engine,NOW,snapshot
    e,_=engine(tmp_path);e.c['execution_policy_version']='PAPER_EXEC_V5'
    e.accept(dict(id='MISSING_SEND_CLOCK',op='snapshot'),dict(ok=True,data=snapshot()),NOW)
    assert e.block=='STALE_FULL_SNAPSHOT' and not e.ready and not e.s['intents']
    events=[json.loads(line) for line in (Path(e.c['evidence_dir'])/'order_events.jsonl').read_text().splitlines()]
    assert events[-1]['kind']=='RPC_STALE_NOT_APPLIED' and events[-1]['request_age_seconds'] is None


@pytest.mark.parametrize('m1_ready',[False,True])
def test_actual_ten_product_report_compares_gm_subscriptions_with_gm_five_only(tmp_path,m1_ready):
    from types import SimpleNamespace
    from collections import Counter
    from test_paper_execution import engine,NOW
    from futures_trend_quant.live.paper_five import FiveRunner
    from futures_trend_quant.live.paper_calendar import PortfolioCalendar
    from futures_trend_quant.live.paper_products import TEN_POOL
    from futures_trend_quant.live.paper_tq import POLICY as TQ
    from futures_trend_quant.live.paper_closed_m1 import POLICY as GM
    e,_=engine(tmp_path);r=FiveRunner.__new__(FiveRunner)
    r.c=dict(e.c,universe=list(TEN_POOL),external_market_policy=TQ,quote_policy_version='GM_HOST_TICK_PRIMARY_V1_20261109',gm_closed_m1_policy=GM)
    r.engine=e;r.evidence=Path(e.c['evidence_dir']);r.notifications=None;r.readonly=False;r.verified=False
    r.started=r.observation_started=NOW;r.max_gap=0;alive=SimpleNamespace(is_alive=lambda:True);r.quote_worker=alive
    r.products={};r.records={};r.calendar=PortfolioCalendar()
    symbols=['DCE.m2701','SHFE.au2612','CZCE.OI701','DCE.y2701','DCE.p2701','CZCE.MA701','DCE.eg2701','DCE.jd2612','CZCE.CJ701','SHFE.ag2702']
    for cs,symbol in zip(TEN_POOL,symbols):
        cal=calendar();r.calendar.register(symbol,cal);r.records[symbol]={}
        r.products[cs]=dict(metadata={'symbol':symbol},calendar=cal,ready=True,coverage=dict(qualified=True,expected_m15=1200,missing_m1=0),frozen=False,fault=None,
            core=SimpleNamespace(progress={},thread=alive),market=SimpleNamespace(thread=alive),counts=Counter(),received={})
    r.tick_subscriptions=set(symbols[:5]);r.m1_subscriptions=set(symbols[:5]) if m1_ready else set();e.s['pause_new']=True
    status=r.report(NOW)
    assert status['ready_products']==10 and status['streaming_subscriptions']==5
    assert 'TICK_SUBSCRIPTIONS_NOT_READY' not in status['entry_readiness']['reasons']
    assert ('CLOSED_M1_SUBSCRIPTIONS_NOT_READY' in status['entry_readiness']['reasons']) is (not m1_ready)
    assert not status['entry_readiness']['account_enabled']
