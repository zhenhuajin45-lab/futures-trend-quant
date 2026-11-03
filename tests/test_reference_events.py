from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backtest.metrics import future_first_touch_direction
from backtest.reference_events import atr_directional_change_reference, score_confirmed_turn_events
from futures_trend_quant.core.models import Bar


def _bars(closes: list[float], *, symbol: str = "TEST.X") -> list[Bar]:
    start = datetime(2026, 1, 5, 9)
    return [
        Bar(
            symbol=symbol,
            frequency="900s",
            bob=start + timedelta(minutes=15 * index),
            eob=start + timedelta(minutes=15 * (index + 1)),
            open=close,
            high=close + 0.25,
            low=close - 0.25,
            close=close,
            volume=1,
        )
        for index, close in enumerate(closes)
    ]


def test_future_first_touch_up_down_double_touch_neither_and_tail() -> None:
    up = _bars([100, 101, 103.2, 100, 100])
    down = _bars([100, 99, 96.8, 100, 100])
    neither = _bars([100, 101, 99, 100, 100])
    assert future_first_touch_direction(up, [1.0] * 5, horizon=2)[0] == 1
    assert future_first_touch_direction(down, [1.0] * 5, horizon=2)[0] == -1
    assert future_first_touch_direction(neither, [1.0] * 5, horizon=2)[0] == 0
    assert future_first_touch_direction(up, [1.0] * 5, horizon=2)[-2:] == [None, None]

    double = _bars([100, 100, 100, 100])
    double[1] = Bar(
        symbol="TEST.X",
        frequency="900s",
        bob=double[1].bob,
        eob=double[1].eob,
        open=100,
        high=104,
        low=96,
        close=100,
        volume=1,
    )
    assert future_first_touch_direction(double, [1.0] * 4, horizon=2)[0] == 0
    assert future_first_touch_direction(double, [1.0] * 4, horizon=2)[-2:] == [None, None]


def test_directional_change_events_are_independent_and_hand_calculable() -> None:
    bars = _bars([100, 104, 110, 109, 107, 103, 106, 109])
    reference = atr_directional_change_reference(
        bars, [1.0] * len(bars), threshold_atr=3.0, observation_bars=2
    )
    assert reference.initial_confirmation_index == 1
    assert list(reference.labels[:5]) == [None, 1, 1, 1, -1]
    assert len(reference.events) == 2
    first = reference.events[0]
    assert (first.old_direction, first.new_direction) == ("bull", "bear")
    assert (first.pivot_index, first.confirmation_index) == (2, 4)
    assert first.threshold_price == pytest.approx(107)
    second = reference.events[1]
    assert (second.old_direction, second.new_direction) == ("bear", "bull")
    assert (second.pivot_index, second.confirmation_index) == (5, 6)
    assert second.observation_complete is False
    assert first.event_id != second.event_id


def test_turn_score_requires_exactly_two_states_and_separates_unresolved_from_censored() -> None:
    bars = _bars([100, 104, 110, 109, 107, 106, 105, 104, 103, 102])
    reference = atr_directional_change_reference(
        bars, [1.0] * len(bars), threshold_atr=3.0, observation_bars=4
    )
    assert len(reference.events) == 1
    event = reference.events[0]
    predicted = ["bull"] * len(bars)
    predicted[event.confirmation_index + 1] = "bear"
    predicted[event.confirmation_index + 2] = "bear"
    score = score_confirmed_turn_events(predicted, reference.events)
    assert score["turn_delay_median"] == 1
    assert score["resolved_turn_events"] == 1

    unresolved = score_confirmed_turn_events(["bull"] * len(bars), reference.events)
    assert unresolved["turn_delay_median"] == 49
    assert unresolved["unresolved_turn_events"] == 1

    censored_reference = atr_directional_change_reference(
        bars[:7], [1.0] * 7, threshold_atr=3.0, observation_bars=4
    )
    censored = score_confirmed_turn_events(["bull"] * 7, censored_reference.events)
    assert censored["turn_events"] == 1
    assert censored["scoreable_turn_events"] == 0
    assert censored["censored_turn_events"] == 1


def test_directional_change_rejects_cross_symbol_sequences() -> None:
    bars = _bars([100, 104]) + _bars([100, 96], symbol="TEST.Y")
    with pytest.raises(ValueError, match="must not cross symbols"):
        future_first_touch_direction(bars, [1.0] * len(bars), horizon=2)
    with pytest.raises(ValueError, match="must not cross symbols"):
        atr_directional_change_reference(bars, [1.0] * len(bars))


def test_future_reference_code_is_not_imported_by_strategy_package() -> None:
    root = Path(__file__).parents[1] / "futures_trend_quant"
    production_source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.py"))
    )
    assert "backtest.reference_events" not in production_source
    assert "future_first_touch_direction" not in production_source
    assert "ATR_DIRECTIONAL_CHANGE_V1" not in production_source
