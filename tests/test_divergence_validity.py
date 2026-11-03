from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from futures_trend_quant.core.facts.divergence import detect_divergence
from futures_trend_quant.core.facts.indicators import IndicatorSet
from futures_trend_quant.core.facts.pipeline import _latest_divergence, _pending_divergence, divergence_is_valid
from futures_trend_quant.core.models import Divergence, MacdSegment, Pivot
from helpers import make_bars


class DivergenceValidityTest(unittest.TestCase):
    def test_divergence_stays_valid_until_price_exceeds_extreme(self) -> None:
        bars = make_bars(12, step=0.0)
        divergence = Divergence("top", "B_hist", 2, 6, 8, 3, ("dif", "hist"), 105.0)
        held = [replace(bar, high=min(bar.high, 104.9), low=min(bar.low, 99.0)) for bar in bars]
        self.assertTrue(divergence_is_valid(divergence, held))
        broken = list(held)
        broken[-1] = replace(broken[-1], high=105.1)
        self.assertFalse(divergence_is_valid(divergence, broken))

    def test_b_hist_rejects_top_divergence_below_zero_axis(self) -> None:
        previous = Pivot(2, 4, "high", 110.0, 2.0)
        current = Pivot(6, 8, "high", 111.0, 2.0)
        dif = (None, None, -1.0, -0.9, -0.8, -0.7, -1.2, -1.0, -0.8)
        hist = (None, None, -0.8, -0.6, -0.5, -0.4, -0.5, -0.3, -0.2)
        indicators = IndicatorSet(dif, dif, dif, dif, hist, dif, tuple(["flat"] * 9),
                                  (MacdSegment("negative", 2, 8, 0.8, 4.0, False),))
        self.assertIsNone(detect_divergence(previous, current, indicators, mode="B_hist"))

    def test_latest_pivot_without_divergence_does_not_resurrect_older_pair(self) -> None:
        bars = make_bars(12, step=0.0)
        values = tuple([1.0] * 12)
        indicators = IndicatorSet(values, values, values, values, values, values, tuple(["flat"] * 12), ())
        pivots = (
            Pivot(2, 4, "high", 105.0, 1.0), Pivot(4, 6, "low", 95.0, 1.0),
            Pivot(6, 8, "high", 106.0, 1.0), Pivot(8, 10, "low", 96.0, 1.0),
            Pivot(9, 11, "high", 107.0, 1.0),
        )
        rules = {"pivot_k": 2, "leg_min_atr": 1.2, "swing_mode": "S_A", "min_span_bars": 5,
                 "max_span_bars": 120, "new_extreme_min_atr": 0.1, "div_mode": "B_hist"}
        with patch("futures_trend_quant.core.facts.pipeline.find_pivots", return_value=pivots), \
             patch("futures_trend_quant.core.facts.pipeline.previous_swing_pair", return_value=(pivots[2], pivots[4])), \
             patch("futures_trend_quant.core.facts.pipeline.detect_divergence", return_value=None) as detect:
            self.assertIsNone(_latest_divergence(bars, indicators, kind="high", rules=rules))
            detect.assert_called_once()

    def test_pending_divergence_requires_new_extreme_lower_peak_and_two_shrinks(self) -> None:
        bars = make_bars(12, step=0.0)
        bars[-1] = replace(bars[-1], high=110.0)
        hist = (None, None, 1.0, 0.8, -0.2, -0.1, None, None, None, 0.6, 0.4, 0.2)
        values = tuple([1.0] * 12)
        indicators = IndicatorSet(values, values, values, values, hist, values, tuple(["flat"] * 12),
                                  (MacdSegment("positive", 2, 3, 1.0, 1.8, True),
                                   MacdSegment("negative", 4, 5, 0.2, 0.3, True),
                                   MacdSegment("positive", 9, 11, 0.6, 1.2, False)))
        rules = {"pivot_k": 2, "leg_min_atr": 1.2, "swing_mode": "S_A"}
        with patch("futures_trend_quant.core.facts.pipeline.find_pivots",
                   return_value=(Pivot(2, 4, "high", 105.0, 1.0),)):
            self.assertTrue(_pending_divergence(bars, indicators, kind="high", rules=rules))


if __name__ == "__main__":
    unittest.main()
