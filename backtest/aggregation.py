from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    """Aggregate M1 bars on exchange wall-clock M5/M15 boundaries.

    Missing M1 observations do not shift every later bar. Any available rows
    in a clock bucket are emitted with the canonical boundary, matching GM's
    native 300s/900s history semantics.
    """

    minutes: int
    frequency: str
    _buckets: dict[str, list[Bar]] = field(default_factory=dict)
    _bucket_ends: dict[str, object] = field(default_factory=dict)
    _ready: dict[str, list[Bar]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.minutes <= 0:
            raise ValueError("minutes must be positive")

    def _combine_at_boundary(self, bucket: list[Bar], boundary) -> Bar:
        return replace(
            combine_bars(bucket, self.frequency),
            bob=boundary - timedelta(minutes=self.minutes),
            eob=boundary,
        )

    def update(self, bar: Bar) -> Bar | None:
        if bar.frequency not in {"60s", "1m", "M1"}:
            raise ValueError(f"expected M1 bar, got {bar.frequency!r}")
        bucket = self._buckets.setdefault(bar.symbol, [])
        ready = self._ready.setdefault(bar.symbol, [])
        if bucket and bar.eob <= bucket[-1].eob:
            raise ValueError("M1 bars must be strictly increasing per symbol")
        minute = bar.eob.minute
        remainder = minute % self.minutes
        bucket_end = bar.eob.replace(second=0, microsecond=0)
        if remainder:
            bucket_end += timedelta(minutes=self.minutes - remainder)
        active_end = self._bucket_ends.get(bar.symbol)
        if bucket and active_end != bucket_end:
            ready.append(self._combine_at_boundary(bucket, active_end))
            bucket.clear()
        self._bucket_ends[bar.symbol] = bucket_end
        bucket.append(bar)
        if bar.eob == bucket_end:
            ready.append(self._combine_at_boundary(bucket, bucket_end))
            bucket.clear()
        return ready.pop(0) if ready else None

    def flush(self, symbol: str) -> Bar | None:
        bucket = self._buckets.get(symbol, [])
        ready = self._ready.setdefault(symbol, [])
        if bucket:
            boundary = self._bucket_ends[symbol]
            ready.append(self._combine_at_boundary(bucket, boundary))
            bucket.clear()
        return ready.pop(0) if ready else None


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
            while True:
                closed = aggregator.flush(symbol)
                if closed is None:
                    break
                output.append(closed)
    return sorted(output, key=lambda item: (item.eob, item.symbol))
