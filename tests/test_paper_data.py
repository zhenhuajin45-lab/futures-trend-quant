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
