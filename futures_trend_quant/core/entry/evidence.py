from __future__ import annotations

from typing import Literal

from ..facts.bar_arithmetic import (
    BarArithmetic,
    calculate_bar_arithmetic,
    is_bearish_engulfing,
    is_bullish_engulfing,
)
from ..facts.divergence import detect_divergence
from ..facts.indicators import IndicatorSet, calculate_indicators
from ..facts.swing import find_pivots, previous_swing_pair
from ..models import Bar
from .models import Direction, EvidenceEvent, EvidenceSnapshot


def _event(kind: str, direction: Direction, bars: list[Bar], index: int) -> EvidenceEvent:
    return EvidenceEvent(kind, direction, bars[index].identity, index, bars[index].eob)


def _arithmetic(bars: list[Bar], indicators: IndicatorSet, index: int) -> BarArithmetic:
    return calculate_bar_arithmetic(
        bars[index],
        bars[index - 1] if index else None,
        indicators.ema34[index],
        indicators.ema34[index - 1] if index else None,
    )


def _latest_divergence(
    bars: list[Bar],
    indicators: IndicatorSet,
    direction: Direction,
    *,
    mode: Literal["A47", "B_hist"],
) -> EvidenceEvent | None:
    kind = "low" if direction == "bull" else "high"
    pivots = find_pivots(bars, indicators.atr14, k=2, leg_min_atr=1.2, mode="S_A")
    latest = next((pivot for pivot in reversed(pivots) if pivot.kind == kind), None)
    if latest is None or latest.defined_at >= len(bars):
        return None
    pair = previous_swing_pair(pivots, bars, current_index=latest.index, kind=kind)
    if pair is None:
        return None
    divergence = detect_divergence(*pair, indicators, mode=mode)
    if divergence is None:
        return None
    # A divergence remains valid until price makes a new extreme.
    tail = bars[divergence.current_index + 1 :]
    invalid = (
        any(bar.low < divergence.extreme_price for bar in tail)
        if direction == "bull"
        else any(bar.high > divergence.extreme_price for bar in tail)
    )
    if invalid:
        return None
    return _event("m5_bottom_divergence" if direction == "bull" else "m5_top_divergence", direction, bars, divergence.defined_at)


def _latest_flatten(
    bars: list[Bar], indicators: IndicatorSet, direction: Direction, lookback: int = 6
) -> EvidenceEvent | None:
    index = len(bars) - 1
    ratio = indicators.slope_ratio[index]
    if ratio is None or not -0.35 < float(ratio) < 0.35:
        return None
    reverse = "down" if direction == "bull" else "up"
    if reverse not in indicators.slope_state[max(0, index - lookback) : index]:
        return None
    return _event("m5_flatten", direction, bars, index)


def _latest_cross_or_engulf(
    bars: list[Bar], indicators: IndicatorSet, direction: Direction, lookback: int = 6
) -> tuple[EvidenceEvent | None, EvidenceEvent | None]:
    cross_event = None
    engulf_event = None
    for index in range(max(1, len(bars) - lookback), len(bars)):
        current = _arithmetic(bars, indicators, index)
        previous = _arithmetic(bars, indicators, index - 1)
        if current.close_ema34_cross == ("up" if direction == "bull" else "down"):
            cross_event = _event("m5_ema_cross", direction, bars, index)
        aligned_engulf = (
            is_bullish_engulfing(current, previous)
            if direction == "bull"
            else is_bearish_engulfing(current, previous)
        )
        if aligned_engulf:
            engulf_event = _event("m5_engulfing", direction, bars, index)
    return cross_event, engulf_event


def _latest_near_extreme_pattern(
    bars: list[Bar], indicators: IndicatorSet, direction: Direction, lookback: int = 6
) -> EvidenceEvent | None:
    start = max(1, len(bars) - lookback)
    window = bars[start:]
    window_extreme = min(bar.low for bar in window) if direction == "bull" else max(bar.high for bar in window)
    latest: EvidenceEvent | None = None
    for index in range(start, len(bars)):
        atr = indicators.atr14[index]
        if atr is None or atr <= 0:
            continue
        current = _arithmetic(bars, indicators, index)
        previous = _arithmetic(bars, indicators, index - 1)
        near = (
            bars[index].low - window_extreme <= atr
            if direction == "bull"
            else window_extreme - bars[index].high <= atr
        )
        if not near:
            continue
        engulf = (
            is_bullish_engulfing(current, previous)
            if direction == "bull"
            else is_bearish_engulfing(current, previous)
        )
        if engulf:
            latest = _event("m5_near_extreme_pattern", direction, bars, index)
            continue
        pinbar = current.lower_pinbar if direction == "bull" else current.upper_pinbar
        if pinbar and index + 1 < len(bars):
            broken = (
                bars[index + 1].close > bars[index].high
                if direction == "bull"
                else bars[index + 1].close < bars[index].low
            )
            if broken:
                latest = _event("m5_near_extreme_pattern", direction, bars, index + 1)
    return latest


def _latest_pullback(
    bars: list[Bar], indicators: IndicatorSet, direction: Direction, lookback: int = 6
) -> EvidenceEvent | None:
    latest = None
    for index in range(max(0, len(bars) - lookback), len(bars)):
        ema, atr = indicators.ema34[index], indicators.atr14[index]
        if ema is not None and atr is not None and abs(bars[index].close - ema) <= 0.5 * atr:
            latest = _event("m5_pullback_ema", direction, bars, index)
    return latest


def _consolidation_and_breakout(
    bars: list[Bar], indicators: IndicatorSet, direction: Direction
) -> tuple[EvidenceEvent | None, EvidenceEvent | None]:
    if len(bars) < 5:
        return None, None

    def is_box(start: int, end: int) -> bool:
        box = bars[start:end]
        atr = indicators.atr14[end - 1]
        if atr is None or atr <= 0:
            return False
        side = all(
            indicators.ema34[i] is not None
            and (bars[i].close > indicators.ema34[i] if direction == "bull" else bars[i].close < indicators.ema34[i])
            for i in range(start, end)
        )
        return side and max(bar.high for bar in box) - min(bar.low for bar in box) <= 1.5 * atr

    current = len(bars) - 1
    prior_start = current - 4
    if is_box(prior_start, current):
        boundary = (
            max(bar.high for bar in bars[prior_start:current])
            if direction == "bull"
            else min(bar.low for bar in bars[prior_start:current])
        )
        broken = bars[current].close > boundary if direction == "bull" else bars[current].close < boundary
        consolidation = _event("m5_consolidation", direction, bars, current - 1)
        if broken:
            return consolidation, _event("m5_consolidation_breakout", direction, bars, current)
    latest_start = len(bars) - 4
    if is_box(latest_start, len(bars)):
        return _event("m5_consolidation", direction, bars, len(bars) - 1), None
    return None, None


def m1_strong_reverse(bars: list[Bar], direction: Direction) -> bool:
    if len(bars) < 50:
        return False
    indicators = calculate_indicators(bars)
    index = len(bars) - 1
    ema, hist = indicators.ema34[index], indicators.hist[index]
    if ema is None or hist is None:
        return False
    if direction == "bull":
        return indicators.slope_state[index] == "down" and bars[index].close < ema and hist < 0
    return indicators.slope_state[index] == "up" and bars[index].close > ema and hist > 0


def find_m1_confirmation(
    bars: list[Bar],
    direction: Direction,
    *,
    after_eob=None,
    lookback: int = 15,
) -> EvidenceEvent | None:
    if len(bars) < 40:
        return None
    indicators = calculate_indicators(bars)
    latest = None
    for index in range(max(1, len(bars) - lookback), len(bars)):
        if after_eob is not None and bars[index].eob <= after_eob:
            continue
        current = _arithmetic(bars, indicators, index)
        previous = _arithmetic(bars, indicators, index - 1)
        ema_cross = current.close_ema34_cross == ("up" if direction == "bull" else "down")
        engulf = (
            is_bullish_engulfing(current, previous)
            if direction == "bull"
            else is_bearish_engulfing(current, previous)
        )
        previous_dif, previous_dea = indicators.dif[index - 1], indicators.dea[index - 1]
        dif, dea = indicators.dif[index], indicators.dea[index]
        macd_cross = False
        if None not in (previous_dif, previous_dea, dif, dea):
            macd_cross = (
                previous_dif <= previous_dea and dif > dea
                if direction == "bull"
                else previous_dif >= previous_dea and dif < dea
            )
        if ema_cross or engulf or macd_cross:
            latest = _event("m1_confirmation", direction, bars, index)
    return latest


def build_entry_evidence(
    m5_bars: list[Bar],
    m1_bars: list[Bar],
    direction: Direction,
    *,
    div_mode: Literal["A47", "B_hist"] = "B_hist",
    m1_after_eob=None,
) -> EvidenceSnapshot:
    if len(m5_bars) < 40:
        return EvidenceSnapshot(direction=direction, m1_strong_reverse=m1_strong_reverse(m1_bars, direction))
    indicators = calculate_indicators(m5_bars)
    cross, engulf = _latest_cross_or_engulf(m5_bars, indicators, direction)
    consolidation, breakout = _consolidation_and_breakout(m5_bars, indicators, direction)
    reverse_direction: Direction = "bear" if direction == "bull" else "bull"
    return EvidenceSnapshot(
        direction=direction,
        m5_divergence=_latest_divergence(m5_bars, indicators, direction, mode=div_mode),
        m5_flatten=_latest_flatten(m5_bars, indicators, direction),
        m5_pattern=_latest_near_extreme_pattern(m5_bars, indicators, direction),
        m5_pullback=_latest_pullback(m5_bars, indicators, direction),
        m5_consolidation=consolidation,
        m5_breakout=breakout,
        m5_ema_cross=cross,
        m5_engulfing=engulf,
        m5_reverse_divergence=_latest_divergence(m5_bars, indicators, reverse_direction, mode=div_mode) is not None,
        m1_confirmation=find_m1_confirmation(m1_bars, direction, after_eob=m1_after_eob),
        m1_strong_reverse=m1_strong_reverse(m1_bars, direction),
        m5_last_sequence=len(m5_bars) - 1,
    )


def recent_m5_event(bars: list[Bar], direction: Direction) -> str | None:
    """Compatibility helper for the original Path-D adapter."""
    if len(bars) < 40:
        return None
    indicators = calculate_indicators(bars)
    cross, engulf = _latest_cross_or_engulf(bars, indicators, direction)
    latest = max((event for event in (cross, engulf) if event is not None), key=lambda event: event.sequence, default=None)
    if latest is None:
        return None
    prefix = "ema_cross_up" if latest.kind == "m5_ema_cross" and direction == "bull" else None
    if latest.kind == "m5_ema_cross" and direction == "bear":
        prefix = "ema_cross_down"
    if latest.kind == "m5_engulfing":
        prefix = "bullish_engulfing" if direction == "bull" else "bearish_engulfing"
    return f"{prefix}:{latest.bar_id}"
