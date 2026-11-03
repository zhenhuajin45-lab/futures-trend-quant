from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .facts.bar_arithmetic import calculate_bar_arithmetic, is_bearish_engulfing, is_bullish_engulfing
from .facts.indicators import calculate_indicators
from .models import Bar


Direction = Literal["bull", "bear"]


@dataclass(frozen=True)
class EntryEvidence:
    accepted: bool
    direction: Direction
    evidence_id: str
    reason: str
    m5_event: str | None
    m1_reverse: bool


def _recent_m5_event(bars: list[Bar], direction: Direction) -> str | None:
    if len(bars) < 40:
        return None
    indicators = calculate_indicators(bars)
    start = max(1, len(bars) - 6)
    for index in range(len(bars) - 1, start - 1, -1):
        current = calculate_bar_arithmetic(bars[index], bars[index - 1], indicators.ema34[index], indicators.ema34[index - 1])
        previous = calculate_bar_arithmetic(
            bars[index - 1], bars[index - 2] if index >= 2 else None, indicators.ema34[index - 1],
            indicators.ema34[index - 2] if index >= 2 else None,
        )
        if direction == "bull":
            if current.close_ema34_cross == "up":
                return f"ema_cross_up:{bars[index].identity}"
            if is_bullish_engulfing(current, previous):
                return f"bullish_engulfing:{bars[index].identity}"
        else:
            if current.close_ema34_cross == "down":
                return f"ema_cross_down:{bars[index].identity}"
            if is_bearish_engulfing(current, previous):
                return f"bearish_engulfing:{bars[index].identity}"
    return None


def _m1_strong_reverse(bars: list[Bar], direction: Direction) -> bool:
    if len(bars) < 50:
        return False
    indicators = calculate_indicators(bars)
    index = len(bars) - 1
    ema_value, hist_value = indicators.ema34[index], indicators.hist[index]
    if ema_value is None or hist_value is None:
        return False
    if direction == "bull":
        return indicators.slope_state[index] == "down" and bars[index].close < ema_value and hist_value < 0
    return indicators.slope_state[index] == "up" and bars[index].close > ema_value and hist_value > 0


def evaluate_path_d(
    *,
    direction: Direction,
    bar_id: str,
    h1_state: str,
    m15_slope_state: str,
    reverse_divergence: bool,
    m5_bars: list[Bar],
    m1_bars: list[Bar],
) -> EntryEvidence:
    evidence_id = f"path_d|{direction}|{bar_id}"
    if h1_state != "strong":
        return EntryEvidence(False, direction, evidence_id, "H1_NOT_STRONG", None, False)
    required_slope = "up" if direction == "bull" else "down"
    if m15_slope_state != required_slope:
        return EntryEvidence(False, direction, evidence_id, "M15_SLOPE_MISMATCH", None, False)
    if reverse_divergence:
        return EntryEvidence(False, direction, evidence_id, "REVERSE_DIVERGENCE", None, False)
    event = _recent_m5_event(m5_bars, direction)
    if event is None:
        return EntryEvidence(False, direction, evidence_id, "M5_TRIGGER_MISSING", None, False)
    evidence_id = f"path_d|{direction}|{event}"
    reverse = _m1_strong_reverse(m1_bars, direction)
    if reverse:
        return EntryEvidence(False, direction, evidence_id, "M1_STRONG_REVERSE", event, True)
    return EntryEvidence(True, direction, evidence_id, "PATH_D_ACCEPTED", event, False)
