from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from ..models import Bar, Divergence, TrendState
from ..trend.state_machine import TrendFacts
from .bar_arithmetic import calculate_bar_arithmetic, is_bearish_engulfing, is_bullish_engulfing
from .divergence import detect_divergence
from .h1_state import H1Absolute, classify_h1_state, relative_h1_state
from .indicators import IndicatorSet, calculate_indicators
from .swing import find_pivots, previous_swing_pair


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


def _h1_bucket_key(value: datetime) -> tuple[date, str, int]:
    minute = value.hour * 60 + value.minute
    if 9 * 60 <= minute < 10 * 60 + 30:
        name, anchor = "day_1", 9 * 60
    elif 10 * 60 + 30 <= minute < 12 * 60:
        name, anchor = "day_2", 10 * 60 + 30
    elif 13 * 60 + 30 <= minute < 15 * 60 + 15:
        name, anchor = "day_3", 13 * 60 + 30
    elif minute >= 21 * 60:
        name, anchor = "night_pm", 21 * 60
    elif minute < 3 * 60:
        name, anchor = "night_am", 0
    else:
        # Deterministic fallback for synthetic tests and newly introduced sessions.
        name, anchor = "clock", value.hour * 60
    return value.date(), name, (minute - anchor) // 60


def aggregate_m15_to_h1(bars: list[Bar]) -> list[Bar]:
    """Build stable four-M15 bars anchored to exchange session segments."""
    result: list[Bar] = []
    bucket: list[Bar] = []
    active_key: tuple[date, str, int] | None = None

    def flush() -> None:
        nonlocal bucket
        if len(bucket) != 4:
            bucket = []
            return
        last = bucket[-1]
        result.append(
            Bar(
                symbol=last.symbol,
                frequency="3600s",
                bob=bucket[0].bob,
                eob=last.eob,
                open=bucket[0].open,
                high=max(item.high for item in bucket),
                low=min(item.low for item in bucket),
                close=last.close,
                volume=sum(item.volume for item in bucket),
                amount=sum(item.amount for item in bucket),
                position=last.position,
                pre_close=bucket[0].pre_close,
            )
        )
        bucket = []

    for bar in bars:
        key = _h1_bucket_key(bar.bob)
        if active_key is not None and key != active_key:
            flush()
        active_key = key
        bucket.append(bar)
    flush()
    return result


def _latest_divergence(
    bars: list[Bar], indicators: IndicatorSet, *, kind: Literal["high", "low"], rules: dict[str, object]
) -> Divergence | None:
    pivots = find_pivots(
        bars,
        indicators.atr14,
        k=int(rules["pivot_k"]),
        leg_min_atr=float(rules["leg_min_atr"]),
        mode=str(rules["swing_mode"]),  # type: ignore[arg-type]
    )
    closed_index = len(bars) - 1
    for current in reversed([item for item in pivots if item.kind == kind and item.defined_at <= closed_index]):
        pair = previous_swing_pair(
            pivots,
            bars,
            current_index=current.index,
            kind=kind,
            min_span_bars=int(rules["min_span_bars"]),
            max_span_bars=int(rules["max_span_bars"]),
            new_extreme_min_atr=float(rules["new_extreme_min_atr"]),
        )
        if pair is None:
            continue
        result = detect_divergence(pair[0], pair[1], indicators, mode=str(rules["div_mode"]))  # type: ignore[arg-type]
        if result is not None:
            return result
    return None


def analyze_market(m15_bars: list[Bar], rules: dict[str, object]) -> MarketAnalysis:
    if len(m15_bars) < 251:
        raise ValueError("M15 warmup requires at least 251 bars")
    indicators = calculate_indicators(m15_bars)
    index = len(m15_bars) - 1
    h1_bars = aggregate_m15_to_h1(m15_bars)
    if len(h1_bars) < 47:
        raise ValueError("H1 warmup requires at least 47 complete bars")
    h1_indicators = calculate_indicators(h1_bars)
    valid_ema = [float(value) for value in h1_indicators.ema34 if value is not None]
    latest_h1_atr = h1_indicators.atr14[-1]
    if len(valid_ema) < 13 or latest_h1_atr is None:
        raise ValueError("H1 indicators unavailable")
    h1 = classify_h1_state(
        valid_ema,
        latest_close=h1_bars[-1].close,
        latest_atr=latest_h1_atr,
        strong_atr=float(rules["h1_strong_atr"]),
        mono_ratio=float(rules["h1_mono_ratio"]),
    )
    current = calculate_bar_arithmetic(
        m15_bars[index], m15_bars[index - 1], indicators.ema34[index], indicators.ema34[index - 1]
    )
    previous = calculate_bar_arithmetic(
        m15_bars[index - 1], m15_bars[index - 2], indicators.ema34[index - 1], indicators.ema34[index - 2]
    )
    previous_hist, current_hist = indicators.hist[index - 1], indicators.hist[index]
    macd_cross: str | None = None
    if previous_hist is not None and current_hist is not None:
        if previous_hist <= 0 < current_hist:
            macd_cross = "up"
        elif previous_hist >= 0 > current_hist:
            macd_cross = "down"
        else:
            macd_cross = "none"
    return MarketAnalysis(
        bar=m15_bars[-1],
        indicators=indicators,
        h1_absolute=h1.absolute,
        top_divergence=_latest_divergence(m15_bars, indicators, kind="high", rules=rules),
        bottom_divergence=_latest_divergence(m15_bars, indicators, kind="low", rules=rules),
        bearish_pattern=is_bearish_engulfing(current, previous),
        bullish_pattern=is_bullish_engulfing(current, previous),
        close_cross=current.close_ema34_cross,
        macd_cross=macd_cross,
    )


def trend_facts(analysis: MarketAnalysis, state: TrendState) -> TrendFacts:
    index = len(analysis.indicators.ema34) - 1
    ema_value = analysis.indicators.ema34[index]
    if ema_value is None:
        close_side: Literal["above", "below", "equal", "unavailable"] = "unavailable"
    elif analysis.bar.close > ema_value:
        close_side = "above"
    elif analysis.bar.close < ema_value:
        close_side = "below"
    else:
        close_side = "equal"
    reference: Literal["bull", "bear"]
    if state.trend_state in ("bull", "bear"):
        reference = state.trend_state
    else:
        reference = "bear" if analysis.indicators.slope_state[index] == "down" else "bull"
    h1_relative = relative_h1_state(analysis.h1_absolute, reference)
    recent_threshold = index - 6
    top = analysis.top_divergence is not None and analysis.top_divergence.defined_at >= recent_threshold
    bottom = analysis.bottom_divergence is not None and analysis.bottom_divergence.defined_at >= recent_threshold
    return TrendFacts(
        bar_id=analysis.bar.identity,
        close=analysis.bar.close,
        high=analysis.bar.high,
        low=analysis.bar.low,
        close_side=close_side,
        slope_state=analysis.indicators.slope_state[index],
        top_divergence=top,
        bottom_divergence=bottom,
        s1_extreme_then_flat=analysis.indicators.slope_state[index] == "flat",
        s2_bearish_pattern=analysis.bearish_pattern,
        s2_bullish_pattern=analysis.bullish_pattern,
        p_close_cross=analysis.close_cross in ("up", "down"),
        p_macd_cross=analysis.macd_cross in ("up", "down"),
        r_bearish_reverse=close_side == "below" and analysis.indicators.slope_state[index] == "down",
        r_bullish_reverse=close_side == "above" and analysis.indicators.slope_state[index] == "up",
        h1_state=h1_relative,
        h1_divergence="none",
    )
