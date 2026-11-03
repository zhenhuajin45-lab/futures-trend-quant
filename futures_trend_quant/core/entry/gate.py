from __future__ import annotations

from dataclasses import dataclass

from ..facts.indicators import calculate_indicators
from ..models import Bar
from .models import Direction


@dataclass(frozen=True)
class GateDecision:
    is_open: bool
    reason: str
    directional: bool
    no_chase: bool
    bar_id: str | None


def evaluate_momentum_gate(
    bars: list[Bar],
    direction: Direction,
    *,
    enabled: bool = True,
    distance_atr: float = 2.3,
    shrink_bars: int = 3,
) -> GateDecision:
    """Evaluate the M15 momentum gate from closed bars only.

    An aligned MACD state opens the directional half of the gate.  Three
    consecutive contractions while price is too far from EMA34 close the
    no-chase half; a later expansion therefore reopens it automatically.
    """

    if distance_atr < 0:
        raise ValueError("distance_atr must be non-negative")
    if shrink_bars < 2:
        raise ValueError("shrink_bars must be at least two")
    if not enabled:
        return GateDecision(True, "DISABLED", True, True, bars[-1].identity if bars else None)
    if len(bars) < 40:
        return GateDecision(False, "INDICATORS_UNAVAILABLE", False, False, None)
    indicators = calculate_indicators(bars)
    index = len(bars) - 1
    dif, dea, hist = indicators.dif[index], indicators.dea[index], indicators.hist[index]
    ema, atr = indicators.ema34[index], indicators.atr14[index]
    if any(value is None for value in (dif, dea, hist, ema, atr)) or float(atr) <= 0:
        return GateDecision(False, "INDICATORS_UNAVAILABLE", False, False, bars[index].identity)

    directional = (
        float(dif) > float(dea) and float(hist) > 0
        if direction == "bull"
        else float(dif) < float(dea) and float(hist) < 0
    )
    if not directional:
        return GateDecision(False, "MACD_NOT_ALIGNED", False, True, bars[index].identity)

    # "Three shortening bars" means three successive contractions and
    # therefore needs four histogram observations.
    recent = indicators.hist[-(shrink_bars + 1) :]
    aligned = all(
        value is not None and (float(value) > 0 if direction == "bull" else float(value) < 0)
        for value in recent
    )
    contracting = aligned and all(
        abs(float(recent[index - 1])) > abs(float(recent[index]))
        for index in range(1, len(recent))
    )
    too_far = abs(bars[index].close - float(ema)) > distance_atr * float(atr)
    no_chase = not (contracting and too_far)
    return GateDecision(
        no_chase,
        "OPEN" if no_chase else "NO_CHASE_CONTRACTION",
        True,
        no_chase,
        bars[index].identity,
    )
