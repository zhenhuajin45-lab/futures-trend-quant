from dataclasses import asdict,replace
from datetime import timedelta,datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json,hashlib,queue
import pandas as pd
import pytest
from test_paper_execution import calendar,NOW
from futures_trend_quant.core.models import Bar
from futures_trend_quant.live.paper_closed_m1 import from_callback,ClosedM1Book,POLICY,SOURCE,restore
from futures_trend_quant.live.paper_tq import POLICY as TQ_POLICY

GM=[('DCE.M','DCE.m2701'),('SHFE.AU','SHFE.au2612'),('CZCE.OI','CZCE.OI701'),('DCE.Y','DCE.y2701'),('DCE.P','DCE.p2701')]


def bar(symbol='DCE.m2701',end=NOW):
    return Bar(symbol,'60s',(end-pd.Timedelta(minutes=1)).to_pydatetime(),end.to_pydatetime(),100,101,99,100,10,1000,20)


@pytest.mark.parametrize('cs,symbol',GM)
def test_official_closed_bar_adapter_five_products_and_no_future_promotion(cs,symbol):
    b=bar(symbol);c={'external_market_policy':TQ_POLICY};record={symbol:{}}
    assert from_callback(asdict(b),record,c,NOW)==(cs,b)
    with pytest.raises(ValueError,match='NOT_CLOSED'):from_callback(asdict(b),record,c,b.eob-timedelta(microseconds=1))
    raw=asdict(b);raw.pop('position')
    with pytest.raises(ValueError,match='UNKNOWN_SIZE'):from_callback(raw,record,c,NOW)


def test_added_tq_product_cannot_be_replaced_by_gm_callback():
    b=bar('DCE.eg2701')
    with pytest.raises(ValueError,match='UNAUTHORIZED_SOURCE'):from_callback(asdict(b),{b.symbol:{}},{'external_market_policy':TQ_POLICY},NOW)


def test_callback_timezone_is_same_canonical_identity_as_gm_history():
    b=bar();raw=asdict(replace(b,bob=b.bob.astimezone(timezone.utc),eob=b.eob.astimezone(timezone.utc)))
    cs,adapted=from_callback(raw,{b.symbol:{}},{},NOW)
    assert adapted.identity==b.identity and adapted==b


def test_callback_waits_for_real_missing_minute_deduplicates_and_rejects_revision():
    last=bar().eob;book=ClosedM1Book(last-timedelta(minutes=2),calendar());late=bar();early=replace(late,bob=late.bob-timedelta(minutes=1),eob=late.eob-timedelta(minutes=1))
    assert book.add([late],{'source':SOURCE})==[] and len(book.pending)==1
    ready=book.add([early],{'source':'GM_HISTORY'})
    assert [b.eob for b,c in ready]==[early.eob,late.eob] and [c['source'] for b,c in ready]==['GM_HISTORY',SOURCE]
    assert book.add([late],{'source':'GM_HISTORY'})==[]
    with pytest.raises(ValueError,match='REVISION'):book.add([replace(late,close=100.5)],{})


def test_callback_calendar_cross_lunch_and_friday_are_not_filled_with_wallclock():
    c=calendar();a=pd.Timestamp('2026-11-05T11:29:00+08:00');book=ClosedM1Book(a.to_pydatetime(),c)
    afternoon=bar(end=pd.Timestamp('2026-11-05T13:31:00+08:00'))
    assert book.add([afternoon],{'source':SOURCE})==[]
    tail=bar(end=pd.Timestamp('2026-11-05T11:30:00+08:00'));ready=book.add([tail],{'source':'GM_HISTORY'})
    assert [b.eob for b,c in ready]==[tail.eob,afternoon.eob]
    invalid=bar(end=pd.Timestamp('2026-11-05T12:00:00+08:00'))
    with pytest.raises(ValueError,match='CALENDAR'):book.add([invalid],{})


def test_actual_host_callback_to_coordinator_to_core_queue_preserves_clock_and_dedupe(tmp_path,monkeypatch):
    from futures_trend_quant.live.paper_five import FiveRunner
    from futures_trend_quant.live import gm_paper_host
    r=FiveRunner.__new__(FiveRunner);r.c={'gm_closed_m1_policy':POLICY,'external_market_policy':TQ_POLICY};r.run_id='HOST_GENERATION'
    r.records={'DCE.m2701':{}};r.host_m1=queue.Queue();r.host_ticks=queue.Queue();r.quote_events=queue.Queue();r.engine=SimpleNamespace(m1=Mock(),pause_entries=Mock());r.event=Mock();r.observe=Mock()
    p=dict(calendar=calendar(),core=SimpleNamespace(progress={'last_closed_m1':str(bar().eob-timedelta(minutes=1))},inbox=queue.Queue()),market=SimpleNamespace(events=queue.Queue()),ready=True,config={'evidence_dir':str(tmp_path)},received={})
    r.products={'DCE.M':p};ctx=SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False)
    monkeypatch.setattr(gm_paper_host,'pd',SimpleNamespace(Timestamp=SimpleNamespace(now=lambda **kw:NOW)))
    gm_paper_host.on_bar(ctx,[asdict(bar())]);FiveRunner.market_events(r,NOW)
    job=p['core'].inbox.get_nowait();assert job[0]=='NEW' and job[1]==[bar()] and job[2]['source']==SOURCE
    assert job[2]['returned_at']==str(NOW) and job[2]['host_run_id']=='HOST_GENERATION'
    p['market'].events.put(('NEW_BARS',[bar()],{'source':'GM_HISTORY'}));FiveRunner.market_events(r,NOW)
    assert p['core'].inbox.empty() and r.engine.m1.call_count==1 and len((tmp_path/'m1_observed.jsonl').read_text().splitlines())==1
    r.engine.pause_entries.assert_not_called()


def test_actual_timer_subscribes_only_old_five_at_tick_and_closed_m1(monkeypatch):
    from futures_trend_quant.live import gm_paper_host as h
    from futures_trend_quant.live.paper_tq import TQ_ADDED
    symbols=[s for cs,s in GM]+['CZCE.MA701','DCE.eg2701','DCE.jd2612','CZCE.CJ701','SHFE.ag2702']
    r=SimpleNamespace(c={'quote_policy_version':'GM_HOST_TICK_PRIMARY_V1_20261109','gm_closed_m1_policy':POLICY,'external_market_policy':TQ_POLICY},records=dict.fromkeys(symbols,{}),tick_subscription_attempted=set(),tick_subscriptions=set(),m1_subscription_attempted=set(),m1_subscriptions=set(),event=Mock(),engine=None,cycle=lambda:False)
    context=SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False,ftq_paper_in_cycle=False)
    subscribe=Mock();monkeypatch.setattr(h,'subscribe',subscribe);h.on_timer(context)
    assert subscribe.call_count==2 and r.tick_subscriptions==r.m1_subscriptions=={s for cs,s in GM}
    call=subscribe.call_args_list[-1];assert call.kwargs['wait_group'] is False and call.kwargs['frequency']=='60s' and not call.kwargs['unsubscribe_previous']
    h.on_timer(context);assert subscribe.call_count==2


def test_used_callback_input_prefix_restores_on_restart_and_tampering_fails(tmp_path):
    b=bar();seed=[replace(b,bob=b.bob-timedelta(minutes=1),eob=b.eob-timedelta(minutes=1))]
    raw=asdict(b);raw['bob']=b.bob.isoformat();raw['eob']=b.eob.isoformat();path=tmp_path/'gm_core_m1.jsonl'
    blob=(json.dumps(dict(policy=POLICY,csymbol='DCE.M',bar=raw))+'\n').encode();path.write_bytes(blob)
    proof=dict(path=str(path.resolve()),bytes=len(blob),sha256=hashlib.sha256(blob).hexdigest());c={'state_dir':str(tmp_path),'csymbol':'DCE.M'}
    assert restore(c,seed,b.eob.isoformat(),proof)==seed+[b]
    path.write_bytes(blob+b'{"partial":')
    assert restore(c,seed,b.eob.isoformat(),proof)==seed+[b]
    path.write_bytes(blob.replace(b'1000',b'2000'))
    with pytest.raises(ValueError,match='PREFIX_CHANGED'):restore(c,seed,b.eob.isoformat(),proof)


def test_installed_gm_sdk_dispatches_barlikedict_to_real_host_adapter(monkeypatch):
    import gm.api  # The installed host loads the public API before SDK dispatch.
    import gm.callback as sdk
    from futures_trend_quant.live import gm_paper_host as host
    from futures_trend_quant.live.paper_five import FiveRunner
    r=FiveRunner.__new__(FiveRunner);r.c={'gm_closed_m1_policy':POLICY};r.records={'DCE.m2701':{}}
    r.run_id='SDK_REAL_DISPATCH';r.host_m1=queue.Queue();r.engine=SimpleNamespace(pause_entries=Mock());r.event=Mock()
    ctx=SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False,has_wait_group=False,
        bar_sub_infos={sdk.BarSubInfo('DCE.m2701','60s')},_add_data_to_cache=Mock(),on_bar_fun=host.on_bar)
    monkeypatch.setattr(sdk,'context',ctx)
    monkeypatch.setattr(host,'pd',SimpleNamespace(Timestamp=SimpleNamespace(now=lambda **kw:NOW)))
    sdk.bar_callback(asdict(bar()))
    assert not r.host_m1.empty(),str(r.event.call_args_list)
    cs,b,clock=r.host_m1.get_nowait()
    assert cs=='DCE.M' and b==bar() and clock['host_run_id']=='SDK_REAL_DISPATCH'
    assert clock['source']==SOURCE and clock['returned_at']==str(NOW)
    ctx._add_data_to_cache.assert_called_once();r.engine.pause_entries.assert_not_called()


def test_real_signal_worker_writes_and_restores_consumed_gm_prefix_without_resetting_runtime(tmp_path):
    from futures_trend_quant.live.paper_data import SignalWorker
    b=bar();previous=replace(b,bob=b.bob-timedelta(minutes=1),eob=b.eob-timedelta(minutes=1))
    c=dict(root=str(tmp_path),state_dir=str(tmp_path/'state'),csymbol='DCE.M',config_sha256='SYNTHETIC',gm_closed_m1_policy=POLICY)
    w=SignalWorker(c,child=True);w.last=previous.eob;w.seed_cache=dict(path='FROZEN_INITIAL',sha256='SEED',symbol=b.symbol)
    w.runtime.used_evidence_ids.add('CONSUMED_BEFORE_RESTART')
    w._record_gm_inputs([b],{previous.identity:previous},dict(source=SOURCE,returned_at=b.eob.isoformat()))
    w.last=b.eob;w._checkpoint();restarted=SignalWorker(c,child=True)
    assert restore(c,[previous],restarted.last,restarted.gm_input_proof)==[previous,b]
    assert restarted.runtime.to_dict()==w.runtime.to_dict() and restarted.seed_cache==w.seed_cache
    with pytest.raises(ValueError,match='LATE_USED_INPUT'):w._record_gm_inputs([previous],{},dict(source=SOURCE))
