from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from ..models import Bar, MacdSegment, SlopeState


Number = float | None


@dataclass(frozen=True)
class IndicatorSet:
    ema34: tuple[Number, ...]
    atr14: tuple[Number, ...]
    dif: tuple[Number, ...]
    dea: tuple[Number, ...]
    hist: tuple[Number, ...]
    slope_ratio: tuple[Number, ...]
    slope_state: tuple[SlopeState, ...]
    macd_segments: tuple[MacdSegment, ...]


def _finite_values(values: Iterable[float]) -> list[float]:
    result = [float(value) for value in values]
    if not result or not all(math.isfinite(value) for value in result):
        raise ValueError("indicator input must contain finite values")
    return result


def ema(values: Iterable[float], period: int) -> tuple[Number, ...]:
    """SMA-seeded EMA with a deterministic TA-style lookback."""
    series = _finite_values(values)
    if period <= 1:
        raise ValueError("period must be greater than 1")
    output: list[Number] = [None] * len(series)
    if len(series) < period:
        return tuple(output)
    current = sum(series[:period]) / period
    output[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for index in range(period, len(series)):
        current = (series[index] - current) * alpha + current
        output[index] = current
    return tuple(output)


def atr(bars: list[Bar], period: int = 14) -> tuple[Number, ...]:
    output: list[Number] = [None] * len(bars)
    if len(bars) <= period:
        return tuple(output)
    true_ranges: list[float] = [bars[0].high - bars[0].low]
    for index in range(1, len(bars)):
        previous_close = bars[index - 1].close
        true_ranges.append(
            max(
                bars[index].high - bars[index].low,
                abs(bars[index].high - previous_close),
                abs(bars[index].low - previous_close),
            )
        )
    current = sum(true_ranges[1 : period + 1]) / period
    output[period] = current
    for index in range(period + 1, len(bars)):
        current = ((period - 1) * current + true_ranges[index]) / period
        output[index] = current
    return tuple(output)


def macd(
    closes: Iterable[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[tuple[Number, ...], tuple[Number, ...], tuple[Number, ...]]:
    values = _finite_values(closes)
    fast = ema(values, fast_period)
    slow = ema(values, slow_period)
    dif: list[Number] = [None] * len(values)
    valid_dif: list[float] = []
    valid_indices: list[int] = []
    for index, (fast_value, slow_value) in enumerate(zip(fast, slow)):
        if fast_value is None or slow_value is None:
            continue
        value = fast_value - slow_value
        dif[index] = value
        valid_dif.append(value)
        valid_indices.append(index)

    dea: list[Number] = [None] * len(values)
    hist: list[Number] = [None] * len(values)
    if len(valid_dif) >= signal_period:
        signal_values = ema(valid_dif, signal_period)
        for source_index, signal_value in zip(valid_indices, signal_values):
            if signal_value is None:
                continue
            dea[source_index] = signal_value
            hist[source_index] = float(dif[source_index]) - signal_value
    return tuple(dif), tuple(dea), tuple(hist)


def slope(
    ema_values: tuple[Number, ...],
    atr_values: tuple[Number, ...],
    lookback: int = 12,
    threshold: float = 0.35,
) -> tuple[tuple[Number, ...], tuple[SlopeState, ...]]:
    ratios: list[Number] = [None] * len(ema_values)
    states: list[SlopeState] = ["unavailable"] * len(ema_values)
    for index in range(lookback, len(ema_values)):
        current = ema_values[index]
        previous = ema_values[index - lookback]
        atr_value = atr_values[index]
        if current is None or previous is None or atr_value is None or atr_value <= 0:
            continue
        ratio = (current - previous) / atr_value
        ratios[index] = ratio
        if ratio >= threshold:
            states[index] = "up"
        elif ratio <= -threshold:
            states[index] = "down"
        else:
            states[index] = "flat"
    return tuple(ratios), tuple(states)


def segment_histogram(hist: tuple[Number, ...]) -> tuple[MacdSegment, ...]:
    segments: list[MacdSegment] = []
    start: int | None = None
    sign: str | None = None
    values: list[float] = []
    last_valid_index: int | None = None

    def flush(end: int, complete: bool) -> None:
        nonlocal start, sign, values
        if start is None or sign is None or not values:
            return
        segments.append(
            MacdSegment(
                sign="positive" if sign == "positive" else "negative",
                start=start,
                end=end,
                peak_abs=max(abs(value) for value in values),
                area_abs=sum(abs(value) for value in values),
                complete=complete,
            )
        )
        start = None
        sign = None
        values = []

    for index, value in enumerate(hist):
        if value is None or value == 0:
            continue
        current_sign = "positive" if value > 0 else "negative"
        if sign is None:
            start = index
            sign = current_sign
        elif current_sign != sign:
            flush(last_valid_index if last_valid_index is not None else index - 1, True)
            start = index
            sign = current_sign
        values.append(value)
        last_valid_index = index
    if last_valid_index is not None:
        flush(last_valid_index, False)
    return tuple(segments)


def calculate_indicators(bars: list[Bar]) -> IndicatorSet:
    if not bars:
        raise ValueError("bars must not be empty")
    closes = [bar.close for bar in bars]
    ema34 = ema(closes, 34)
    atr14 = atr(bars, 14)
    dif, dea, hist = macd(closes)
    slope_ratio, slope_state = slope(ema34, atr14)
    return IndicatorSet(
        ema34=ema34,
        atr14=atr14,
        dif=dif,
        dea=dea,
        hist=hist,
        slope_ratio=slope_ratio,
        slope_state=slope_state,
        macd_segments=segment_histogram(hist),
    )
