from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from ..models import Bar, Divergence, TrendState
from ..trend.state_machine import TrendFacts
from .bar_arithmetic import calculate_bar_arithmetic, is_bearish_engulfing, is_bullish_engulfing
from .divergence import detect_divergence
from .h1_state import H1Absolute, classify_h1_state, relative_h1_state
from .indicators import IndicatorSet, calculate_indicators
from .swing import find_pivots, previous_swing_pair


@dataclass(frozen=True)
class H1Aggregate:
    bar: Bar
    source_bar_ids: tuple[str, ...]
    trading_day: date


@dataclass(frozen=True)
class MarketAnalysis:
    bar: Bar
    indicators: IndicatorSet
    h1_absolute: H1Absolute
    top_divergence: Divergence | None
    bottom_divergence: Divergence | None
    bearish_pattern: bool
    bullish_pattern: bool
    close_cross: str | None
    macd_cross: str | None
    m15_bars: tuple[Bar, ...] = ()
    h1_top_divergence: Divergence | None = None
    h1_bottom_divergence: Divergence | None = None
    h1_top_pending: bool = False
    h1_bottom_pending: bool = False


def trading_day_for_bar(bar: Bar) -> date:
    result = bar.bob.date() + timedelta(days=1) if bar.bob.hour >= 20 else bar.bob.date()
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def _aggregate_group(group: list[Bar], trading_day: date) -> H1Aggregate:
    last = group[-1]
    return H1Aggregate(
        Bar(
            symbol=last.symbol,
            frequency="3600s",
            bob=group[0].bob,
            eob=last.eob,
            open=group[0].open,
            high=max(item.high for item in group),
            low=min(item.low for item in group),
            close=last.close,
            volume=sum(item.volume for item in group),
            amount=sum(item.amount for item in group),
            position=last.position,
            pre_close=group[0].pre_close,
        ),
        tuple(item.identity for item in group),
        trading_day,
    )


def aggregate_m15_to_h1_with_coverage(bars: list[Bar]) -> list[H1Aggregate]:
    """Group bars by trading-day occurrence order and cover every input once."""
    by_day: dict[tuple[str, date], list[Bar]] = {}
    order: list[tuple[str, date]] = []
    for bar in bars:
        key = (bar.symbol, trading_day_for_bar(bar))
        if key not in by_day:
            by_day[key] = []
            order.append(key)
        by_day[key].append(bar)
    result: list[H1Aggregate] = []
    for key in order:
        groups = [by_day[key][index : index + 4] for index in range(0, len(by_day[key]), 4)]
        if len(groups) > 1 and len(groups[-1]) < 4:
            groups[-2].extend(groups.pop())
        result.extend(_aggregate_group(group, key[1]) for group in groups)
    return result


def aggregate_m15_to_h1(bars: list[Bar]) -> list[Bar]:
    return [item.bar for item in aggregate_m15_to_h1_with_coverage(bars)]


def divergence_is_valid(divergence: Divergence | None, bars: list[Bar]) -> bool:
    if divergence is None or divergence.current_index >= len(bars):
        return False
    subsequent = bars[divergence.current_index + 1 :]
    if divergence.kind == "top":
        return not any(bar.high > divergence.extreme_price for bar in subsequent)
    return not any(bar.low < divergence.extreme_price for bar in subsequent)


def _latest_divergence(
    bars: list[Bar], indicators: IndicatorSet, *, kind: Literal["high", "low"], rules: dict[str, object]
) -> Divergence | None:
    pivots = find_pivots(
        bars, indicators.atr14, k=int(rules["pivot_k"]),
        leg_min_atr=float(rules["leg_min_atr"]), mode=str(rules["swing_mode"]),  # type: ignore[arg-type]
    )
    candidates = [item for item in pivots if item.kind == kind and item.defined_at < len(bars)]
    if not candidates:
        return None
    current = candidates[-1]
    pair = previous_swing_pair(
        pivots, bars, current_index=current.index, kind=kind,
        min_span_bars=int(rules["min_span_bars"]), max_span_bars=int(rules["max_span_bars"]),
        new_extreme_min_atr=float(rules["new_extreme_min_atr"]),
    )
    if pair is None:
        return None
    return detect_divergence(pair[0], pair[1], indicators, mode=str(rules["div_mode"]))  # type: ignore[arg-type]


def _pending_divergence(
    bars: list[Bar], indicators: IndicatorSet, *, kind: Literal["high", "low"], rules: dict[str, object]
) -> bool:
    if len(bars) < 3:
        return False
    pivots = find_pivots(
        bars, indicators.atr14, k=int(rules["pivot_k"]),
        leg_min_atr=float(rules["leg_min_atr"]), mode=str(rules["swing_mode"]),  # type: ignore[arg-type]
    )
    previous = next((item for item in reversed(pivots) if item.kind == kind), None)
    if previous is None:
        return False
    current_extreme = bars[-1].high if kind == "high" else bars[-1].low
    if (kind == "high" and current_extreme <= previous.price) or (kind == "low" and current_extreme >= previous.price):
        return False
    latest = indicators.hist[-3:]
    sign = 1 if kind == "high" else -1
    if any(value is None or float(value) * sign <= 0 for value in latest):
        return False
    if not (abs(float(latest[0])) > abs(float(latest[1])) > abs(float(latest[2]))):
        return False
    previous_segment = next((s for s in indicators.macd_segments if s.start <= previous.index <= s.end), None)
    current_segment = next((s for s in indicators.macd_segments if s.start <= len(bars) - 1 <= s.end), None)
    required_sign = "positive" if kind == "high" else "negative"
    return bool(previous_segment and current_segment and previous_segment is not current_segment
                and previous_segment.sign == required_sign and current_segment.sign == required_sign
                and current_segment.peak_abs < previous_segment.peak_abs)


def analyze_market(m15_bars: list[Bar], rules: dict[str, object]) -> MarketAnalysis:
    if len(m15_bars) < 251:
        raise ValueError("M15 warmup requires at least 251 bars")
    indicators = calculate_indicators(m15_bars)
    index = len(m15_bars) - 1
    h1_bars = aggregate_m15_to_h1(m15_bars)
    if len(h1_bars) < 47:
        raise ValueError("H1 warmup requires at least 47 complete bars")
    h1_indicators = calculate_indicators(h1_bars)
    h1_top = _latest_divergence(h1_bars, h1_indicators, kind="high", rules=rules)
    h1_bottom = _latest_divergence(h1_bars, h1_indicators, kind="low", rules=rules)
    h1_top_pending = _pending_divergence(h1_bars, h1_indicators, kind="high", rules=rules)
    h1_bottom_pending = _pending_divergence(h1_bars, h1_indicators, kind="low", rules=rules)
    valid_ema = [float(value) for value in h1_indicators.ema34 if value is not None]
    latest_h1_atr = h1_indicators.atr14[-1]
    if len(valid_ema) < 13 or latest_h1_atr is None:
        raise ValueError("H1 indicators unavailable")
    preliminary_h1 = classify_h1_state(
        valid_ema, latest_close=h1_bars[-1].close, latest_atr=latest_h1_atr,
        strong_atr=float(rules["h1_strong_atr"]), mono_ratio=float(rules["h1_mono_ratio"]),
    )
    if preliminary_h1.absolute.startswith("bull"):
        relevant_confirmed = divergence_is_valid(h1_top, h1_bars)
        relevant_pending = h1_top_pending
    elif preliminary_h1.absolute.startswith("bear"):
        relevant_confirmed = divergence_is_valid(h1_bottom, h1_bars)
        relevant_pending = h1_bottom_pending
    else:
        relevant_confirmed = relevant_pending = False
    h1_div: Literal["none", "pending", "confirmed"] = (
        "confirmed" if relevant_confirmed else "pending" if relevant_pending else "none"
    )
    h1 = classify_h1_state(
        valid_ema, latest_close=h1_bars[-1].close, latest_atr=latest_h1_atr, divergence=h1_div,
        strong_atr=float(rules["h1_strong_atr"]), mono_ratio=float(rules["h1_mono_ratio"]),
    )
    current = calculate_bar_arithmetic(m15_bars[index], m15_bars[index - 1], indicators.ema34[index], indicators.ema34[index - 1])
    previous = calculate_bar_arithmetic(m15_bars[index - 1], m15_bars[index - 2], indicators.ema34[index - 1], indicators.ema34[index - 2])
    previous_hist, current_hist = indicators.hist[index - 1], indicators.hist[index]
    macd_cross: str | None = None
    if previous_hist is not None and current_hist is not None:
        macd_cross = "up" if previous_hist <= 0 < current_hist else "down" if previous_hist >= 0 > current_hist else "none"
    return MarketAnalysis(
        bar=m15_bars[-1], indicators=indicators, h1_absolute=h1.absolute,
        top_divergence=_latest_divergence(m15_bars, indicators, kind="high", rules=rules),
        bottom_divergence=_latest_divergence(m15_bars, indicators, kind="low", rules=rules),
        bearish_pattern=is_bearish_engulfing(current, previous), bullish_pattern=is_bullish_engulfing(current, previous),
        close_cross=current.close_ema34_cross, macd_cross=macd_cross, m15_bars=tuple(m15_bars),
        h1_top_divergence=h1_top, h1_bottom_divergence=h1_bottom,
        h1_top_pending=h1_top_pending, h1_bottom_pending=h1_bottom_pending,
    )


def _close_side(close: float, ema_value: float | None) -> Literal["above", "below", "equal", "unavailable"]:
    if ema_value is None:
        return "unavailable"
    return "above" if close > ema_value else "below" if close < ema_value else "equal"


def _s1(analysis: MarketAnalysis, state: TrendState, direction: Literal["bull", "bear"]) -> bool:
    bars = list(analysis.m15_bars)
    if not bars or state.trend_extreme is None:
        return False
    states = analysis.indicators.slope_state
    for index in range(max(0, len(bars) - 12), len(bars) - 1):
        hit = bars[index].high >= state.trend_extreme if direction == "bull" else bars[index].low <= state.trend_extreme
        expected = "up" if direction == "bull" else "down"
        if hit and any(states[j - 1] == expected and states[j] == "flat" for j in range(index + 1, len(bars))):
            return True
    return False


def _pattern(analysis: MarketAnalysis, bearish: bool) -> bool:
    bars = list(analysis.m15_bars)
    if len(bars) < 2:
        return analysis.bearish_pattern if bearish else analysis.bullish_pattern
    begin = max(1, len(bars) - 6)
    segment_high = max(bar.high for bar in bars[begin - 1:])
    segment_low = min(bar.low for bar in bars[begin - 1:])
    for index in range(begin, len(bars)):
        atr_value = analysis.indicators.atr14[index]
        if atr_value is None:
            continue
        current = calculate_bar_arithmetic(bars[index], bars[index - 1], None)
        previous = calculate_bar_arithmetic(bars[index - 1], None, None)
        near = segment_high - bars[index].high <= atr_value if bearish else bars[index].low - segment_low <= atr_value
        engulfing = is_bearish_engulfing(current, previous) if bearish else is_bullish_engulfing(current, previous)
        pinbar_break = index + 1 < len(bars) and (
            (current.upper_pinbar and bars[index + 1].close < bars[index].low) if bearish
            else (current.lower_pinbar and bars[index + 1].close > bars[index].high)
        )
        if near and (engulfing or pinbar_break):
            return True
    return False


def trend_facts(analysis: MarketAnalysis, state: TrendState) -> TrendFacts:
    index = len(analysis.indicators.ema34) - 1
    close_side = _close_side(analysis.bar.close, analysis.indicators.ema34[index])
    reference: Literal["bull", "bear"] = state.trend_state if state.trend_state in ("bull", "bear") else (
        "bear" if analysis.indicators.slope_state[index] == "down" else "bull"
    )
    h1_relative = relative_h1_state(analysis.h1_absolute, reference)
    bars = list(analysis.m15_bars)
    top = divergence_is_valid(analysis.top_divergence, bars) if bars else analysis.top_divergence is not None
    bottom = divergence_is_valid(analysis.bottom_divergence, bars) if bars else analysis.bottom_divergence is not None
    bearish_pattern, bullish_pattern = _pattern(analysis, True), _pattern(analysis, False)
    slope_state = analysis.indicators.slope_state[index]
    h1_bars = aggregate_m15_to_h1(bars)
    if reference == "bull":
        p_close = close_side == "below"
        p_macd = bool(top and analysis.macd_cross == "down" and analysis.top_divergence and index > analysis.top_divergence.defined_at)
        r_bearish = close_side == "below" and slope_state in ("flat", "down") and bearish_pattern
        r_bullish = False
        h1_divergence = "confirmed" if divergence_is_valid(analysis.h1_top_divergence, h1_bars) else "pending" if analysis.h1_top_pending else "none"
        distance = (float(analysis.indicators.ema34[index]) - analysis.bar.close) if analysis.indicators.ema34[index] is not None else 0.0
    else:
        p_close = close_side == "above"
        p_macd = bool(bottom and analysis.macd_cross == "up" and analysis.bottom_divergence and index > analysis.bottom_divergence.defined_at)
        r_bearish = False
        r_bullish = close_side == "above" and slope_state in ("flat", "up") and bullish_pattern
        h1_divergence = "confirmed" if divergence_is_valid(analysis.h1_bottom_divergence, h1_bars) else "pending" if analysis.h1_bottom_pending else "none"
        distance = analysis.bar.close - float(analysis.indicators.ema34[index]) if analysis.indicators.ema34[index] is not None else 0.0
    atr_value = analysis.indicators.atr14[index]
    strong_break = atr_value is not None and distance >= 0.5 * float(atr_value)
    unblock_allowed = False
    if state.blocked_dir is not None:
        old_trend: Literal["bull", "bear"] = "bear" if state.blocked_dir == "bull" else "bull"
        old_h1 = relative_h1_state(analysis.h1_absolute, old_trend)
        reverse_div = ((divergence_is_valid(analysis.h1_top_divergence, h1_bars) or analysis.h1_top_pending)
                       if old_trend == "bull" else
                       (divergence_is_valid(analysis.h1_bottom_divergence, h1_bars) or analysis.h1_bottom_pending))
        unblock_allowed = old_h1 in ("flat", "reverse") or (old_h1 == "weakening" and reverse_div)
    return TrendFacts(
        bar_id=analysis.bar.identity, close=analysis.bar.close, high=analysis.bar.high, low=analysis.bar.low,
        close_side=close_side, slope_state=slope_state, top_divergence=top, bottom_divergence=bottom,
        s1_extreme_then_flat=_s1(analysis, state, reference), s2_bearish_pattern=bearish_pattern,
        s2_bullish_pattern=bullish_pattern, p_close_cross=p_close, p_macd_cross=p_macd,
        r_bearish_reverse=r_bearish, r_bullish_reverse=r_bullish, h1_state=h1_relative,
        h1_divergence=h1_divergence, strong_break_atr_met=strong_break, unblock_allowed=unblock_allowed,
    )
