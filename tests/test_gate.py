from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from futures_trend_quant.core.entry import evaluate_momentum_gate
from helpers import make_bars


def _indicators(*, hist, close=100.0, ema=90.0, atr=1.0):
    size = 60
    padded = [0.1] * (size - len(hist)) + list(hist)
    return SimpleNamespace(
        dif=tuple([1.0] * size),
        dea=tuple([0.0] * size),
        hist=tuple(padded),
        ema34=tuple([ema] * size),
        atr14=tuple([atr] * size),
    )


def test_gate_closes_after_three_shortening_bars_when_price_too_far() -> None:
    bars = make_bars(60)
    with patch(
        "futures_trend_quant.core.entry.gate.calculate_indicators",
        return_value=_indicators(hist=(4.0, 3.0, 2.0, 1.0)),
    ):
        decision = evaluate_momentum_gate(bars, "bull", distance_atr=2.3)
    assert not decision.is_open
    assert decision.reason == "NO_CHASE_CONTRACTION"


def test_gate_reopens_when_histogram_expands() -> None:
    bars = make_bars(60)
    with patch(
        "futures_trend_quant.core.entry.gate.calculate_indicators",
        return_value=_indicators(hist=(4.0, 3.0, 2.0, 3.0)),
    ):
        decision = evaluate_momentum_gate(bars, "bull", distance_atr=2.3)
    assert decision.is_open
    assert decision.reason == "OPEN"


def test_gate_is_fail_closed_when_macd_direction_is_wrong() -> None:
    bars = make_bars(60)
    indicators = _indicators(hist=(1.0, 1.0, 1.0, 1.0))
    indicators.dif = tuple([-1.0] * 60)
    with patch("futures_trend_quant.core.entry.gate.calculate_indicators", return_value=indicators):
        decision = evaluate_momentum_gate(bars, "bull")
    assert not decision.is_open
    assert decision.reason == "MACD_NOT_ALIGNED"
