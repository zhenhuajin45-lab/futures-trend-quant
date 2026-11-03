from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math
import unittest

from futures_trend_quant.core.facts_state import (
    FactsState,
    FactsStateBook,
    StableDivergence,
    latest_valid_divergence,
    update_facts_state,
)
from helpers import make_bars


RULES = {
    "pivot_k": 2, "leg_min_atr": 0.2, "swing_mode": "S_B", "min_span_bars": 5,
    "max_span_bars": 120, "new_extreme_min_atr": 0.0, "div_mode": "B_hist",
}


class FactsStateWindowInvarianceTest(unittest.TestCase):
    def test_confirmed_facts_are_not_rewritten_when_window_moves(self) -> None:
        bars = []
        for index, bar in enumerate(make_bars(380, step=0.0)):
            close = 100.0 + math.sin(index / 5.0) * 4.0 + index * 0.01
            bars.append(replace(bar, open=close - 0.1, close=close, high=close + 0.4, low=close - 0.4))
        state = update_facts_state(FactsState(), bars[:320], RULES)
        snapshot = deepcopy(state)
        update_facts_state(state, bars[-120:], RULES)
        self.assertEqual(state.pivots[: len(snapshot.pivots)], snapshot.pivots)
        self.assertEqual(state.divergences[: len(snapshot.divergences)], snapshot.divergences)

    def test_state_book_roundtrip_keeps_csymbol_states_isolated(self) -> None:
        book = FactsStateBook()
        au = book.for_csymbol("SHFE.AU")
        au.observed_bar_ids.add("au-bar")
        ag = book.for_csymbol("SHFE.AG")
        ag.observed_bar_ids.add("ag-bar")
        restored = FactsStateBook.from_dict(book.to_dict())
        self.assertEqual(restored.for_csymbol("SHFE.AU").observed_bar_ids, {"au-bar"})
        self.assertEqual(restored.for_csymbol("SHFE.AG").observed_bar_ids, {"ag-bar"})

    def test_old_divergence_is_invalidated_even_after_defining_pivot_leaves_window(self) -> None:
        state = FactsState(divergences=[StableDivergence(
            "top", "B_hist", "old-1", "old-2", "old-3", 3, ("dif", "hist"), 105.0
        )])
        bars = make_bars(4, step=0.0)
        bars[-1] = replace(bars[-1], high=106.0)
        self.assertIsNone(latest_valid_divergence(state, bars, "top"))


if __name__ == "__main__":
    unittest.main()
