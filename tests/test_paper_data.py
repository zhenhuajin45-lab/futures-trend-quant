"""Paper data eligibility and unchanged core warmup contracts."""
from datetime import timedelta
import pandas as pd
import pytest
from test_paper_execution import calendar,NOW
from futures_trend_quant.core.models import Bar
from futures_trend_quant.core.facts.pipeline import analyze_market
from futures_trend_quant.config import load_config,default_config_path
from futures_trend_quant.live.paper_data import CORE_MIN_M15,complete_bars,MarketWorker
from futures_trend_quant.live.paper_runner import warmup_coverage


def minute(t,price=100):return Bar('DCE.m2701','60s',t-pd.Timedelta(minutes=1),t,price,price+1,price-1,price,1)


def test_coverage_uses_complete_expected_minutes_not_number_of_aggregates():
    c=calendar();times=pd.date_range('2026-11-05T09:31:00+08:00',periods=30,freq='min')
    bars=[minute(t) for t in times]
    assert warmup_coverage(bars,c,required=2)['qualified']
    missing=warmup_coverage(bars[:5]+bars[6:],c,required=2)
    assert not missing['qualified'] and missing['missing_m1']==1
    assert len(complete_bars(bars[:5]+bars[6:],15))==1


def test_actual_market_loader_discards_unclosed_future_m1_before_core(tmp_path):
    w=MarketWorker(dict(evidence_dir=str(tmp_path)));w.symbol='DCE.m2701'
    def row(t,p):return dict(symbol=w.symbol,frequency='60s',bob=t-pd.Timedelta(minutes=1),eob=t,open=p,high=p+1,low=p-1,close=p,volume=1,amount=1,position=1)
    f=pd.DataFrame([row(NOW,100),row(NOW+pd.Timedelta(minutes=1),1000000)])
    result=w.bars(f,NOW+pd.Timedelta(seconds=5))
    assert len(result)==1 and result[0].close==100 and result[0].eob<=NOW
    from datetime import datetime
    assert type(result[0].bob) is datetime and type(result[0].eob) is datetime


def test_timestamp_normalization_preserves_closed_bar_values_ids_and_h1(tmp_path):
    from futures_trend_quant.core.facts.pipeline import aggregate_m15_to_h1_with_coverage
    from futures_trend_quant.data.gm_bars import bars_from_records
    w=MarketWorker(dict(evidence_dir=str(tmp_path)));w.symbol='DCE.m2701'
    times=pd.date_range('2026-11-05T09:01:00+08:00',periods=60,freq='min')
    rows=[dict(symbol=w.symbol,frequency='60s',bob=t-pd.Timedelta(minutes=1),eob=t,open=100,high=101,low=99,close=100,volume=1) for t in times]
    before=bars_from_records(rows);after=w.bars(pd.DataFrame(rows),times[-1]+pd.Timedelta(seconds=5))
    assert before==after and [b.identity for b in before]==[b.identity for b in after]
    assert aggregate_m15_to_h1_with_coverage(complete_bars(before,15))==aggregate_m15_to_h1_with_coverage(complete_bars(after,15))


def test_fractional_minute_is_not_silently_rounded_during_normalization(tmp_path):
    w=MarketWorker(dict(evidence_dir=str(tmp_path)));w.symbol='DCE.m2701';t=NOW+pd.Timedelta(nanoseconds=1)
    f=pd.DataFrame([dict(symbol=w.symbol,frequency='60s',bob=t-pd.Timedelta(minutes=1),eob=t,open=100,high=101,low=99,close=100)])
    with pytest.raises(ValueError,match='whole minute'):w.bars(f,NOW+pd.Timedelta(minutes=1))


def test_cold_start_matches_existing_251_bar_core_requirement():
    rules=load_config(default_config_path()).raw['rules']
    start=pd.Timestamp('2026-09-01T09:00:00+08:00')
    bars=[Bar('DCE.m2701','900s',(start+pd.Timedelta(minutes=15*i)).to_pydatetime(),
              (start+pd.Timedelta(minutes=15*(i+1))).to_pydatetime(),100,101,99,100,1) for i in range(CORE_MIN_M15)]
    assert CORE_MIN_M15==251
    with pytest.raises(ValueError,match='251'):analyze_market(bars[:-1],rules)
    assert analyze_market(bars,rules).bar.identity==bars[-1].identity


@pytest.mark.parametrize('period',[5,15])
def test_complete_aggregation_matches_original_and_rejects_duplicates(period):
    from backtest.aggregation import aggregate_bars
    times=pd.date_range('2026-11-05T09:01:00+08:00',periods=75,freq='min')
    bars=[minute(t,100+j*.1) for j,t in enumerate(times)]
    assert complete_bars(bars,period)==aggregate_bars(bars,period,str(period*60)+'s')
    duplicate=bars[:period-1]+[bars[0]]
    assert not complete_bars(duplicate,period)


def test_native_market_calls_are_isolated_from_risk_timer_process(tmp_path):
    from multiprocessing.process import BaseProcess
    from futures_trend_quant.live.paper_data import _market_process
    w=MarketWorker(dict(evidence_dir=str(tmp_path)))
    assert isinstance(w.thread,BaseProcess)
    assert w.thread._target is _market_process
    assert w.thread._args[1] is w.events
    w.events.close()


def test_core_rebuild_is_separate_from_execution_process(tmp_path):
    from multiprocessing.process import BaseProcess
    from futures_trend_quant.live.paper_data import SignalWorker,_signal_process
    w=SignalWorker(dict(state_dir=str(tmp_path)))
    assert isinstance(w.thread,BaseProcess) and w.thread._target is _signal_process
    assert w.thread._args[1] is w.inbox and w.thread._args[2] is w.outbox
    w.inbox.close();w.outbox.close()


def test_restart_after_roll_keeps_old_contract_for_confirmed_exits(tmp_path):
    from futures_trend_quant.live.paper_data import pinned_contract
    c=dict(state_dir=str(tmp_path),config_sha256='frozen')
    assert pinned_contract(c,[dict(symbol='DCE.m2701')])=='DCE.m2701'
    assert pinned_contract(c,[dict(symbol='DCE.m2705')])=='DCE.m2701'
    assert pinned_contract(c,[])=='DCE.m2701'
    with pytest.raises(ValueError,match='pin/config'):pinned_contract(dict(c,config_sha256='changed'),[])


def test_review_schedule_keeps_bad_data_days_and_excludes_initial_partial_day():
    from futures_trend_quant.live.paper_runner import elapsed_review_slots
    c=calendar()
    assert elapsed_review_slots(c,'2026-11-05T12:47:00+08:00','2026-11-06T15:01:00+08:00')==['2026-11-06']
    assert elapsed_review_slots(c,'2026-11-05T12:47:00+08:00','2026-11-09T15:01:00+08:00')==['2026-11-06','2026-11-09']


def test_review_schedule_does_not_finish_before_day_close_grace():
    from futures_trend_quant.live.paper_runner import elapsed_review_slots
    assert not elapsed_review_slots(calendar(),'2026-11-05T12:47:00+08:00','2026-11-06T15:00:59+08:00')


def test_frozen_seed_roundtrip_keeps_original_prefix_and_rejects_changed_raw(tmp_path):
    import hashlib
    from futures_trend_quant.live.paper_data import SignalWorker
    root=tmp_path/'_strategy_runtime';cache=root/'data/cache/paper_market/seed';cache.mkdir(parents=True)
    state=root/'state';state.mkdir();ev=root/'evidence';ev.mkdir()
    t=NOW;row=dict(symbol='DCE.m2701',frequency='60s',bob=t-pd.Timedelta(minutes=1),eob=t,
                  open=100,high=101,low=99,close=100,volume=1)
    path=cache/'initial_m1.parquet';pd.DataFrame([row]).to_parquet(path)
    seed=dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),symbol=row['symbol'],cutoff=str(t+pd.Timedelta(seconds=5)))
    config=dict(root=str(root),state_dir=str(state),evidence_dir=str(ev))
    first=SignalWorker(config,child=True);first.seed_cache=seed;first.last=t
    first.runtime.used_evidence_ids.add('ALREADY_CONSUMED');first._checkpoint()
    restored=SignalWorker(config,child=True)
    assert restored.seed_cache==seed and restored.runtime.used_evidence_ids=={'ALREADY_CONSUMED'}
    assert restored._cache_bars(restored.seed_cache)[0].close==100
    original=path.read_bytes();path.write_bytes(original+b'CHANGED')
    with pytest.raises(ValueError,match='cache identity changed'):restored._cache_bars(seed)
    path.write_bytes(original)
    outside=tmp_path/'outside.parquet';outside.write_bytes(original)
    with pytest.raises(ValueError,match='cache identity changed'):restored._cache_bars(dict(seed,path=str(outside)))
