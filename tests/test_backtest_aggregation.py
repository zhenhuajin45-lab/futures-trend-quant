from datetime import datetime, timedelta

from backtest.aggregation import SequentialAggregator, aggregate_bars
from futures_trend_quant.core.models import Bar


def _bar(index: int, *, symbol: str = "SHFE.au2606", start: datetime | None = None) -> Bar:
    start = start or datetime(2026, 10, 20, 9, 0)
    bob = start + timedelta(minutes=index)
    close = 100.0 + index
    return Bar(
        symbol=symbol,
        frequency="60s",
        bob=bob,
        eob=bob + timedelta(minutes=1),
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=10.0 + index,
        amount=1000.0,
        position=100.0,
        pre_close=99.0,
    )


def test_aggregate_m1_to_m5_ohlcv() -> None:
    combined = aggregate_bars([_bar(i) for i in range(5)], 5, "300s")
    assert len(combined) == 1
    bar = combined[0]
    assert bar.open == 99.5
    assert bar.close == 104.0
    assert bar.high == 105.0
    assert bar.low == 99.0
    assert bar.volume == sum(10.0 + i for i in range(5))


def test_gap_resets_incomplete_bucket() -> None:
    aggregator = SequentialAggregator(5, "300s")
    assert aggregator.update(_bar(0)) is None
    assert aggregator.update(_bar(1)) is None
    assert aggregator.update(_bar(10)) is None
    for index in range(11, 14):
        assert aggregator.update(_bar(index)) is None
    assert aggregator.update(_bar(14)) is not None
