from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import unittest

from futures_trend_quant.core.facts.bar_arithmetic import (
    calculate_bar_arithmetic,
    is_bearish_engulfing,
)
from futures_trend_quant.core.facts.divergence import detect_divergence
from futures_trend_quant.core.facts.h1_state import classify_h1_state, relative_h1_state
from futures_trend_quant.core.facts.indicators import IndicatorSet, calculate_indicators, ema
from futures_trend_quant.core.facts.pipeline import aggregate_m15_to_h1_with_coverage
from futures_trend_quant.core.facts.swing import find_pivots, previous_swing_pair
from futures_trend_quant.core.models import MacdSegment, Pivot

from helpers import make_bars


class FactsTest(unittest.TestCase):
    def test_bar_arithmetic_engulfing_and_cross(self) -> None:
        previous, current = make_bars(2)
        previous = replace(previous, open=100.0, close=101.0, high=101.2, low=99.8)
        current = replace(current, open=101.1, close=99.9, high=101.3, low=99.7)
        previous_arithmetic = calculate_bar_arithmetic(previous, None, 100.5)
        current_arithmetic = calculate_bar_arithmetic(current, previous, 100.2, 100.5)
        self.assertTrue(current_arithmetic.body_contains_previous)
        self.assertTrue(is_bearish_engulfing(current_arithmetic, previous_arithmetic))
        self.assertEqual(current_arithmetic.close_ema34_cross, "down")

    def test_indicator_shapes_and_slope(self) -> None:
        bars = make_bars(320)
        indicators = calculate_indicators(bars)
        self.assertEqual(len(indicators.ema34), 320)
        self.assertIsNotNone(indicators.ema34[-1])
        self.assertIsNotNone(indicators.atr14[-1])
        self.assertIsNotNone(indicators.hist[-1])
        self.assertEqual(indicators.slope_state[-1], "up")
        self.assertAlmostEqual(ema([1, 2, 3, 4], 3)[2], 2.0)

    def test_swing_pair_does_not_cross_more_extreme_point(self) -> None:
        bars = make_bars(30, step=0.0)
        prices = [100.0] * 30
        prices[5], prices[12], prices[20] = 105.0, 108.0, 107.0
        lows = [99.0] * 30
        lows[8], lows[16] = 95.0, 96.0
        adjusted = [
            replace(bar, high=max(prices[index], bar.open, bar.close), low=min(lows[index], bar.open, bar.close))
            for index, bar in enumerate(bars)
        ]
        atr_values = tuple([1.0] * 30)
        pivots = find_pivots(adjusted, atr_values, mode="S_B")
        current_high = next(pivot for pivot in pivots if pivot.kind == "high" and pivot.index == 20)
        self.assertIsNone(previous_swing_pair(pivots, adjusted, current_index=current_high.index, kind="high"))

    def test_b_hist_divergence_channels(self) -> None:
        previous = Pivot(index=2, defined_at=4, kind="high", price=110.0, atr=2.0)
        current = Pivot(index=6, defined_at=8, kind="high", price=111.0, atr=2.0)
        values = (None, None, 2.0, 1.8, 1.5, 1.4, 1.7, 1.5, 1.2)
        hist = (None, None, 1.0, 0.8, 0.6, 0.5, 0.6, 0.4, 0.2)
        indicators = IndicatorSet(
            ema34=values,
            atr14=values,
            dif=values,
            dea=values,
            hist=hist,
            slope_ratio=values,
            slope_state=tuple(["flat"] * 9),
            macd_segments=(MacdSegment("positive", 2, 8, 1.0, 4.1, False),),
        )
        result = detect_divergence(previous, current, indicators, mode="B_hist")
        self.assertIsNotNone(result)
        self.assertEqual(result.quality, 3)
        self.assertEqual(result.channels, ("dif", "hist"))

    def test_a47_allows_current_ongoing_segment_but_not_previous(self) -> None:
        previous = Pivot(index=2, defined_at=4, kind="high", price=110.0, atr=2.0)
        current = Pivot(index=7, defined_at=9, kind="high", price=111.0, atr=2.0)
        dif = (None, None, 3.0, 2.5, -0.2, -0.3, 2.0, 2.0, 1.5)
        dea = (None, None, 2.5, 2.3, -0.1, -0.2, 1.8, 1.8, 1.4)
        indicators = IndicatorSet(
            ema34=dif,
            atr14=dif,
            dif=dif,
            dea=dea,
            hist=(None, None, 1.0, 0.8, -0.1, -0.2, 0.7, 0.5, 0.4),
            slope_ratio=dif,
            slope_state=tuple(["flat"] * 9),
            macd_segments=(
                MacdSegment("positive", 2, 3, 1.0, 1.8, True),
                MacdSegment("negative", 4, 5, 0.2, 0.3, True),
                MacdSegment("positive", 6, 8, 0.7, 1.6, False),
            ),
        )
        result = detect_divergence(previous, current, indicators, mode="A47")
        self.assertIsNotNone(result)
        self.assertEqual(result.quality, 3)

        invalid_previous = IndicatorSet(
            **{**indicators.__dict__, "macd_segments": (
                MacdSegment("positive", 2, 3, 1.0, 1.8, False),
                *indicators.macd_segments[1:],
            )}
        )
        self.assertIsNone(detect_divergence(previous, current, invalid_previous, mode="A47"))

    def test_h1_classification_and_relative_state(self) -> None:
        result = classify_h1_state(
            [100.0 + index * 0.1 for index in range(13)],
            latest_close=102.0,
            latest_atr=1.0,
        )
        self.assertEqual(result.absolute, "bull_strong")
        self.assertEqual(relative_h1_state(result.absolute, "bear"), "reverse")

    def test_h1_aggregation_covers_every_source_bar(self) -> None:
        sessions = []
        for day in range(60):
            for bar in make_bars(4):
                sessions.append(
                    replace(
                        bar,
                        bob=bar.bob + timedelta(days=day),
                        eob=bar.eob + timedelta(days=day),
                    )
                )
        aggregates = aggregate_m15_to_h1_with_coverage(sessions)
        covered = [bar_id for aggregate in aggregates for bar_id in aggregate.source_bar_ids]
        self.assertCountEqual(covered, [bar.identity for bar in sessions])
        self.assertEqual(len(covered), len(set(covered)))


if __name__ == "__main__":
    unittest.main()
