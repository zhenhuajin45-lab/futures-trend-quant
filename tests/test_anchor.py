from __future__ import annotations

from futures_trend_quant.core.entry import select_anchor
from futures_trend_quant.core.models import Pivot
from helpers import make_bars


def test_range20_anchor_uses_directional_extreme() -> None:
    bars = make_bars(30)
    selection = select_anchor(bars, "bull", mode="range20")
    expected = min(range(10, 30), key=lambda index: bars[index].low)
    assert selection is not None
    assert selection.anchor_index == expected
    assert selection.anchor_price == bars[expected].low


def test_swing_anchor_does_not_move_during_pullback_before_a_line_break() -> None:
    bars = make_bars(20)
    pivots = (
        Pivot(4, 6, "low", bars[4].low, 1.0),
        Pivot(8, 10, "high", bars[8].high + 100.0, 1.0),
        Pivot(12, 14, "low", bars[12].low - 10.0, 1.0),
    )
    initial = select_anchor(bars[:12], "bull", mode="swing", pivots=pivots)
    assert initial is not None
    updated = select_anchor(bars, "bull", mode="swing", pivots=pivots, previous=initial)
    assert updated is not None
    assert updated.anchor_index == 4


def test_swing_anchor_remembers_break_until_new_low_is_confirmed() -> None:
    bars = make_bars(20)
    a_price = bars[-1].close - 1.0
    pivots = (
        Pivot(4, 6, "low", bars[4].low, 1.0),
        Pivot(8, 10, "high", a_price, 1.0),
    )
    state = select_anchor(bars[:12], "bull", mode="swing", pivots=pivots)
    assert state is not None
    broken = select_anchor(bars, "bull", mode="swing", pivots=pivots, previous=state)
    assert broken is not None and broken.a_line_broken

    new_pivot = Pivot(15, 17, "low", bars[15].low, 1.0)
    advanced = select_anchor(
        bars, "bull", mode="swing", pivots=pivots + (new_pivot,), previous=broken
    )
    assert advanced is not None
    assert advanced.anchor_index == 15
