from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
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
    bucket_start: datetime | None = None
    bucket_end: datetime | None = None
    closed: bool = False
    complete: bool = False
    calendar_qualified: bool = True
    known_at: datetime | None = None


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
    finalized_h1_absolute: H1Absolute | None = None
    finalized_h1_eob: datetime | None = None
    h1_source: dict[str, object] | None = None
    h1_top_active: bool | None = None
    h1_bottom_active: bool | None = None


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


def _h1_bounds(bar: Bar) -> tuple[datetime, datetime, bool]:
    """Fixed commodity session boundaries; short session tails are explicit.

    Out-of-session research fixtures use fixed wall hours, marked unqualified.
    They cannot authorize a paper entry. No observed-count or future-bar boundary.
    """
    bob = bar.bob
    if bob.tzinfo is not None:
        bob = bob.astimezone(timezone(timedelta(hours=8)))
    midnight = bob.replace(hour=0, minute=0, second=0, microsecond=0)
    minute = bob.hour * 60 + bob.minute
    sessions = [(540, 615), (630, 690), (810, 900)]
    product = ''.join(c for c in bar.symbol.split('.')[-1] if c.isalpha()).upper()
    if product in ('AU', 'AG'):
        sessions += [(0, 150), (1260, 1440)]
    elif product not in ('JD', 'CJ', 'LH', 'LC'):
        sessions += [(1260, 1380)]
    for start, end in sessions:
        if start <= minute < end:
            begin = start + ((minute - start) // 60) * 60
            return (midnight + timedelta(minutes=begin),
                    midnight + timedelta(minutes=min(begin + 60, end)), True)
    begin = bob.replace(minute=0, second=0, microsecond=0)
    return begin, begin + timedelta(hours=1), False


def aggregate_m15_to_h1_with_coverage(bars: list[Bar]) -> list[H1Aggregate]:
    """Cover every input, but never publish incomplete/provisional buckets as H1.

    A closed bucket retains its ID, end, source set and OHLC as a prefix grows.
    Missing M15 bars are not excused by a later observation or the next session.
    """
    from dataclasses import replace
    if not bars:
        return []
    cutoff = max(b.eob for b in bars)
    groups: dict[tuple[str, datetime, datetime, bool], list[Bar]] = {}
    for bar in bars:
        start, end, qualified = _h1_bounds(bar)
        groups.setdefault((bar.symbol, start, end, qualified), []).append(bar)
    result = []
    for (symbol, start, end, qualified), group in sorted(groups.items(), key=lambda x: (x[0][1], x[0][0])):
        group.sort(key=lambda b: b.bob)
        expected = {start + timedelta(minutes=i) for i in range(0, int((end-start).total_seconds()/60), 15)}
        complete = (len(group) == len(expected) and {b.bob for b in group} == expected
                    and all(b.eob-b.bob == timedelta(minutes=15) for b in group))
        closed = cutoff >= end
        result.append(replace(_aggregate_group(group, trading_day_for_bar(group[0])),
                              bucket_start=start, bucket_end=end, closed=closed, complete=complete,
                              calendar_qualified=qualified, known_at=end if closed and complete else None))
    return result


def aggregate_m15_to_h1(bars: list[Bar]) -> list[Bar]:
    return [item.bar for item in aggregate_m15_to_h1_with_coverage(bars) if item.closed and item.complete]


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


class _H1Unavailable(ValueError):
    pass


def _classify_h1_history(
    h1_bars: list[Bar], rules: dict[str, object], confirmed_override: tuple[bool, bool] | None = None
) -> tuple[H1Absolute, Divergence | None, Divergence | None, bool, bool]:
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
        raise _H1Unavailable("H1 indicators unavailable")
    if latest_h1_atr <= 0:
        raise _H1Unavailable("latest_atr must be positive")
    preliminary_h1 = classify_h1_state(
        valid_ema, latest_close=h1_bars[-1].close, latest_atr=latest_h1_atr,
        strong_atr=float(rules["h1_strong_atr"]), mono_ratio=float(rules["h1_mono_ratio"]),
    )
    if preliminary_h1.absolute.startswith("bull"):
        relevant_confirmed = divergence_is_valid(h1_top, h1_bars) if confirmed_override is None else confirmed_override[0]
        relevant_pending = h1_top_pending
    elif preliminary_h1.absolute.startswith("bear"):
        relevant_confirmed = divergence_is_valid(h1_bottom, h1_bars) if confirmed_override is None else confirmed_override[1]
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
    return h1.absolute, h1_top, h1_bottom, h1_top_pending, h1_bottom_pending


def _last_h1_is_final(m15_bar: Bar) -> bool:
    # All contracts in the frozen domestic commodity universe end their day
    # session at 15:00 Shanghai time. Overnight and midday breaks are not EOD.
    end = m15_bar.eob
    if end.tzinfo is not None:
        end = end.astimezone(timezone(timedelta(hours=8)))
    return end.time() == time(15) and end.date() == trading_day_for_bar(m15_bar)


def _finalized_entry_h1(
    h1_bars: list[Bar], m15_bar: Bar, rules: dict[str, object], provisional: H1Absolute | None = None
) -> tuple[H1Absolute | None, datetime | None]:
    # Input is already the complete, closed prefix. Trend and entry share it.
    closed = h1_bars
    if len(closed) < 47:
        return None, None
    try:
        absolute = provisional if provisional is not None else _classify_h1_history(closed, rules)[0]
    except _H1Unavailable:
        # A moving partial bucket may have the first positive ATR. It can
        # warm the historical trend, while closed H1 remains unknown to entry.
        return None, None
    return absolute, closed[-1].eob


def finalized_entry_h1(m15_bars: list[Bar], rules: dict[str, object]) -> tuple[H1Absolute | None, datetime | None]:
    """Return the entry-only H1 classification and its latest completed source EOB."""
    if len(m15_bars) < 251:
        return None, None
    return _finalized_entry_h1(aggregate_m15_to_h1(m15_bars), m15_bars[-1], rules)


def analyze_market(m15_bars: list[Bar], rules: dict[str, object]) -> MarketAnalysis:
    if len(m15_bars) < 251:
        raise ValueError("M15 warmup requires at least 251 bars")
    indicators = calculate_indicators(m15_bars)
    index = len(m15_bars) - 1
    h1_bars = aggregate_m15_to_h1(m15_bars)
    h1, h1_top, h1_bottom, h1_top_pending, h1_bottom_pending = _classify_h1_history(h1_bars, rules)
    finalized_h1, finalized_eob = _finalized_entry_h1(h1_bars, m15_bars[-1], rules, h1)
    current = calculate_bar_arithmetic(m15_bars[index], m15_bars[index - 1], indicators.ema34[index], indicators.ema34[index - 1])
    previous = calculate_bar_arithmetic(m15_bars[index - 1], m15_bars[index - 2], indicators.ema34[index - 1], indicators.ema34[index - 2])
    previous_hist, current_hist = indicators.hist[index - 1], indicators.hist[index]
    macd_cross: str | None = None
    if previous_hist is not None and current_hist is not None:
        macd_cross = "up" if previous_hist <= 0 < current_hist else "down" if previous_hist >= 0 > current_hist else "none"
    return MarketAnalysis(
        bar=m15_bars[-1], indicators=indicators, h1_absolute=h1,
        top_divergence=_latest_divergence(m15_bars, indicators, kind="high", rules=rules),
        bottom_divergence=_latest_divergence(m15_bars, indicators, kind="low", rules=rules),
        bearish_pattern=is_bearish_engulfing(current, previous), bullish_pattern=is_bullish_engulfing(current, previous),
        close_cross=current.close_ema34_cross, macd_cross=macd_cross, m15_bars=tuple(m15_bars),
        h1_top_divergence=h1_top, h1_bottom_divergence=h1_bottom,
        h1_top_pending=h1_top_pending, h1_bottom_pending=h1_bottom_pending,
        finalized_h1_absolute=finalized_h1, finalized_h1_eob=finalized_eob,
        h1_source={
            "policy": "SESSION_FIXED_CLOSED_V2", "cutoff": m15_bars[-1].eob.isoformat(),
            "trend_eob": h1_bars[-1].eob.isoformat(), "entry_eob": finalized_eob.isoformat() if finalized_eob else None,
            "calendar_qualified": all(x.calendar_qualified for x in aggregate_m15_to_h1_with_coverage(m15_bars) if x.closed and x.complete),
            "source_bar_ids": next(x.source_bar_ids for x in reversed(aggregate_m15_to_h1_with_coverage(m15_bars)) if x.closed and x.complete),
        },
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
    h1_top_active = divergence_is_valid(analysis.h1_top_divergence, h1_bars) if analysis.h1_top_active is None else analysis.h1_top_active
    h1_bottom_active = divergence_is_valid(analysis.h1_bottom_divergence, h1_bars) if analysis.h1_bottom_active is None else analysis.h1_bottom_active
    if reference == "bull":
        p_close = close_side == "below"
        p_macd = bool(top and analysis.macd_cross == "down" and analysis.top_divergence and index > analysis.top_divergence.defined_at)
        r_bearish = close_side == "below" and slope_state in ("flat", "down") and bearish_pattern
        r_bullish = False
        h1_divergence = "confirmed" if h1_top_active else "pending" if analysis.h1_top_pending else "none"
        distance = (float(analysis.indicators.ema34[index]) - analysis.bar.close) if analysis.indicators.ema34[index] is not None else 0.0
    else:
        p_close = close_side == "above"
        p_macd = bool(bottom and analysis.macd_cross == "up" and analysis.bottom_divergence and index > analysis.bottom_divergence.defined_at)
        r_bearish = False
        r_bullish = close_side == "above" and slope_state in ("flat", "up") and bullish_pattern
        h1_divergence = "confirmed" if h1_bottom_active else "pending" if analysis.h1_bottom_pending else "none"
        distance = analysis.bar.close - float(analysis.indicators.ema34[index]) if analysis.indicators.ema34[index] is not None else 0.0
    atr_value = analysis.indicators.atr14[index]
    strong_break = atr_value is not None and distance >= 0.5 * float(atr_value)
    unblock_allowed = False
    if state.blocked_dir is not None:
        old_trend: Literal["bull", "bear"] = "bear" if state.blocked_dir == "bull" else "bull"
        old_h1 = relative_h1_state(analysis.h1_absolute, old_trend)
        reverse_div = ((h1_top_active or analysis.h1_top_pending)
                       if old_trend == "bull" else
                       (h1_bottom_active or analysis.h1_bottom_pending))
        unblock_allowed = old_h1 in ("flat", "reverse") or (old_h1 == "weakening" and reverse_div)
    return TrendFacts(
        bar_id=analysis.bar.identity, close=analysis.bar.close, high=analysis.bar.high, low=analysis.bar.low,
        close_side=close_side, slope_state=slope_state, top_divergence=top, bottom_divergence=bottom,
        s1_extreme_then_flat=_s1(analysis, state, reference), s2_bearish_pattern=bearish_pattern,
        s2_bullish_pattern=bullish_pattern, p_close_cross=p_close, p_macd_cross=p_macd,
        r_bearish_reverse=r_bearish, r_bullish_reverse=r_bullish, h1_state=h1_relative,
        h1_divergence=h1_divergence, strong_break_atr_met=strong_break, unblock_allowed=unblock_allowed,
    )
