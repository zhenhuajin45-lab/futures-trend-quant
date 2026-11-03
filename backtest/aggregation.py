from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable

from futures_trend_quant.core.models import Bar


def combine_bars(bars: list[Bar], frequency: str) -> Bar:
    if not bars:
        raise ValueError("cannot combine an empty bar sequence")
    symbol = bars[0].symbol
    if any(bar.symbol != symbol for bar in bars):
        raise ValueError("all bars in a bucket must have the same symbol")
    ordered = sorted(bars, key=lambda item: item.eob)
    return Bar(
        symbol=symbol,
        frequency=frequency,
        bob=ordered[0].bob,
        eob=ordered[-1].eob,
        open=ordered[0].open,
        high=max(item.high for item in ordered),
        low=min(item.low for item in ordered),
        close=ordered[-1].close,
        volume=sum(item.volume for item in ordered),
        amount=sum(item.amount for item in ordered),
        position=ordered[-1].position,
        pre_close=ordered[0].pre_close,
    )


@dataclass
class SequentialAggregator:
    """Aggregate contiguous M1 bars by sequence, resetting buckets at gaps.

    Sequence aggregation mirrors the strategy's intended trading-session
    semantics: breaks between sessions do not create synthetic empty minutes.
    Incomplete buckets are not emitted unless :meth:`flush` is called.
    """

    minutes: int
    frequency: str
    _buckets: dict[str, list[Bar]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.minutes <= 0:
            raise ValueError("minutes must be positive")

    def update(self, bar: Bar) -> Bar | None:
        if bar.frequency not in {"60s", "1m", "M1"}:
            raise ValueError(f"expected M1 bar, got {bar.frequency!r}")
        bucket = self._buckets.setdefault(bar.symbol, [])
        if bucket:
            gap = bar.eob - bucket[-1].eob
            if gap <= timedelta(0):
                raise ValueError("M1 bars must be strictly increasing per symbol")
            if gap > timedelta(minutes=1, seconds=1):
                bucket.clear()
        bucket.append(bar)
        if len(bucket) < self.minutes:
            return None
        result = combine_bars(bucket, self.frequency)
        bucket.clear()
        return result

    def flush(self, symbol: str) -> Bar | None:
        bucket = self._buckets.get(symbol, [])
        if not bucket:
            return None
        result = combine_bars(bucket, self.frequency)
        bucket.clear()
        return result


def aggregate_bars(
    bars: Iterable[Bar], minutes: int, frequency: str, *, include_partial: bool = False
) -> list[Bar]:
    aggregator = SequentialAggregator(minutes, frequency)
    output: list[Bar] = []
    symbols: set[str] = set()
    for bar in sorted(bars, key=lambda item: (item.eob, item.symbol)):
        symbols.add(bar.symbol)
        closed = aggregator.update(bar)
        if closed is not None:
            output.append(closed)
    if include_partial:
        for symbol in sorted(symbols):
            closed = aggregator.flush(symbol)
            if closed is not None:
                output.append(closed)
    return sorted(output, key=lambda item: (item.eob, item.symbol))
