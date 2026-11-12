"""R13 actual consumer boundaries. Synthetic unit data never reaches a broker."""
from dataclasses import asdict,replace
from types import SimpleNamespace
from datetime import timedelta
from pathlib import Path
import hashlib,json,queue
import pandas as pd
import pytest
from test_gm_closed_m1 import GM,bar,POLICY as CLOSED,SOURCE
from test_paper_execution import NOW,calendar
from futures_trend_quant.live.paper_data import SignalWorker
from futures_trend_quant.live.paper_gm_inputs import POLICY as INPUT,persist
from futures_trend_quant.live.paper_gm_handoff import POLICY as HANDOFF,LatestQuotes,ordered_products
from futures_trend_quant.live.paper_closed_m1 import restore


@pytest.mark.parametrize('cs,symbol',GM)
def test_initial_consumed_window_survives_second_worker_and_preserves_runtime(tmp_path,cs,symbol):
    b=bar(symbol);previous=replace(b,bob=b.bob-timedelta(minutes=1),eob=b.eob-timedelta(minutes=1))
    c=dict(root=str(tmp_path),state_dir=str(tmp_path/'state'),evidence_dir=str(tmp_path/'evidence'),csymbol=cs,
        config_sha256='SYNTHETIC',gm_closed_m1_policy=CLOSED,gm_consumed_input_policy=INPUT)
    first=SignalWorker(c,child=True);first.runtime.used_evidence_ids.add('KEEP_CONSUMED_ID')
    first.seed_cache=persist(c,[previous],{})
    first.gm_input_cache=persist(c,[previous,b],dict(initial='ACTUALLY_CONSUMED_INITIAL_CACHE'))
    first.last=b.eob;first._checkpoint();second=SignalWorker(c,child=True)
    assert second.runtime.to_dict()==first.runtime.to_dict() and second.seed_cache==first.seed_cache
    restored=restore(c,second._cache_bars(second.gm_input_cache),second.last,second.gm_input_proof)
    assert restored==[previous,b] and second.gm_input_cache['historical_known_at']=='UNKNOWN'
    path=Path(second.gm_input_cache['path']);path.write_bytes(path.read_bytes()+b'tamper')
    with pytest.raises(ValueError,match='identity changed'):second._cache_bars(second.gm_input_cache)


def test_legacy_missing_consumed_prefix_still_rejects(tmp_path):
    from futures_trend_quant.live.paper_tq import append
    b=bar();old=replace(b,bob=b.bob-timedelta(minutes=2),eob=b.eob-timedelta(minutes=2))
    c=dict(root=str(tmp_path),state_dir=str(tmp_path),csymbol='DCE.M')
    raw=asdict(old);raw.update(bob=old.bob.isoformat(),eob=old.eob.isoformat())
    append(tmp_path/'gm_core_m1.jsonl',dict(policy=CLOSED,csymbol='DCE.M',bar=raw))
    raw=(tmp_path/'gm_core_m1.jsonl').read_bytes();proof=dict(path=str(tmp_path/'gm_core_m1.jsonl'),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError,match='USED_INPUT_PREFIX_MISSING'):restore(c,[old],b.eob,proof)


def test_quote_mailbox_bounds_flood_and_keeps_original_clocks():
    q=LatestQuotes()
    for n in range(10000):q.put_nowait(('DCE.m2701',dict(created_at=(NOW+timedelta(microseconds=n)).isoformat(),source='GM_HOST_TICK_CALLBACK'),str(n)))
    q.put_nowait(('DCE.m2701',dict(created_at=NOW.isoformat(),source='GM_BULK_CURRENT_POLL'),'OLDER'))
    assert q.qsize()==1 and q.replaced==9999 and q.older==1
    assert q.get_nowait()[2]=='9999'
    with pytest.raises(queue.Empty):q.get_nowait()


def test_clock_degraded_retry_is_earlier_without_extending_original_sample():
    from futures_trend_quant.live.paper_clock_health import next_probe_delay,RETRY_POLICY,MAX_AGE
    row=dict(status='HEALTHY',probe_degraded=True,sample_monotonic=100.)
    assert next_probe_delay(row,130)==30 and next_probe_delay(row,130,RETRY_POLICY)==5
    assert row['sample_monotonic']==100 and MAX_AGE==90


def test_bounded_pending_drain_never_discards_the_undrained_tail(tmp_path,monkeypatch):
    from futures_trend_quant.live.paper_five import FiveRunner
    from futures_trend_quant.live.paper_cycle_work import CycleWork
    from unittest.mock import Mock
    b=bar();r=FiveRunner.__new__(FiveRunner);r.c=dict(gm_handoff_policy=HANDOFF);r.event=Mock()
    pending={str(n):(replace(b,eob=b.eob+timedelta(minutes=n)),dict(returned_at=str(n))) for n in range(30)}
    r.products={'DCE.M':dict(ready=True,gm_warmup_pending=pending)};r._cycle_work=CycleWork()
    applied=[];monkeypatch.setattr(FiveRunner,'apply_m1',lambda self,cs,bars,clocks,now:applied.append(clocks['returned_at']))
    r.drain_gm_pending('DCE.M',NOW)
    assert 0<len(applied)<=8 and len(pending)==30-len(applied) and applied==[str(n) for n in range(len(applied))]


def test_gm_and_secondary_budgets_and_outer_rotation_are_independent():
    from futures_trend_quant.live.paper_tq import POLICY as TQ
    from futures_trend_quant.live.paper_cycle_work import CycleWork,take
    r=SimpleNamespace(c=dict(external_market_policy=TQ,gm_handoff_policy=HANDOFF),products=dict.fromkeys([x[0] for x in GM]+['CZCE.MA','DCE.EG','DCE.JD','CZCE.CJ','SHFE.AG']))
    first=ordered_products(r);second=ordered_products(r)
    assert first[:5]==[x[0] for x in GM] and second[:5]==first[1:5]+first[:1]
    mono=[0.];r._cycle_work=CycleWork(lambda:mono[0])
    assert take(r,'CORE_DATA','DCE.JD');mono[0]=1
    assert not take(r,'CORE_DATA','DCE.JD') and take(r,'CORE_DATA','DCE.M')


def test_callback_warmup_retains_exact_bar_then_real_book_consumes_once(tmp_path):
    from futures_trend_quant.live.paper_five import FiveRunner
    from unittest.mock import Mock
    b=bar();r=FiveRunner.__new__(FiveRunner);r.c=dict(gm_closed_m1_policy=CLOSED,gm_handoff_policy=HANDOFF)
    r.run_id='SANDBOX';r.event=Mock();r.observe=Mock();r.engine=SimpleNamespace(m1=Mock())
    p=dict(calendar=calendar(),ready=False,core=SimpleNamespace(progress=dict(status='REBUILDING_S0'),inbox=queue.Queue()),
        config=dict(evidence_dir=str(tmp_path)),received={})
    r.products={'DCE.M':p};proof=dict(source=SOURCE,returned_at=NOW.isoformat(),host_run_id='SANDBOX')
    r.apply_m1('DCE.M',[b],proof,NOW);assert p['core'].inbox.empty() and len(p['gm_warmup_pending'])==1
    with pytest.raises(ValueError,match='REVISION'):r.apply_m1('DCE.M',[replace(b,close=100.5)],proof,NOW)
    p['ready']=True;p['core'].progress['last_closed_m1']=(b.eob-timedelta(minutes=1)).isoformat()
    r.drain_gm_pending('DCE.M',NOW)
    assert p['gm_warmup_pending']=={}
    job=p['core'].inbox.get_nowait();assert job[1]==[b] and job[2]['returned_at']==proof['returned_at']
    r.apply_m1('DCE.M',[b],proof,NOW);assert p['core'].inbox.empty() and r.engine.m1.call_count==1


def test_actual_priority_cycle_services_gm_before_secondary_and_keeps_risk(tmp_path):
    from test_paper_pool_rotation import cycle_fixture
    from futures_trend_quant.live.paper_tq import POLICY as TQ
    from futures_trend_quant.live.paper_symbol_health import POLICY as ISOLATION
    r=cycle_fixture(tmp_path);r.c.update(gm_handoff_policy=HANDOFF,external_market_policy=TQ,symbol_market_policy=ISOLATION)
    calls=[];r.market_events=lambda now,csymbol=None:calls.append(('market',csymbol))
    r.core_events=lambda now,csymbol=None:calls.append(('core',csymbol))
    r.supervise_clock=lambda now:None;r.restore_strategy_fills=lambda:None
    r.engine.invalidate_candidates=lambda *a:None;r.engine.tick=lambda now:calls.append(('risk',None))
    prototype=next(iter(r.products.values()));r.products['CZCE.MA']=dict(prototype);r.products['SHFE.AG']=dict(prototype)
    assert r.cycle() is False
    assert calls[0]==('market','__FEEDS_ONLY__')
    assert [cs for stage,cs in calls if stage=='core'][:5]==[cs for cs,s in GM]
    assert calls[-1]==('risk',None)
