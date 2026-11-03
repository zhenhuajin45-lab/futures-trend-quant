from __future__ import annotations

from dataclasses import replace
import unittest

from futures_trend_quant.core.facts.indicators import IndicatorSet
from futures_trend_quant.core.facts.pipeline import MarketAnalysis, trend_facts
from futures_trend_quant.core.models import Divergence, TrendState
from helpers import make_bars


def analysis(*, close: float, macd_cross: str, divergence: Divergence | None,
             h1_divergence: Divergence | None = None, h1_pending: bool = False) -> MarketAnalysis:
    bars = [replace(bar, open=100.0, close=100.0, high=100.2, low=99.8) for bar in make_bars(20, step=0.0)]
    bars[-1] = replace(bars[-1], open=close + 0.1, close=close, high=max(close + 0.3, 100.3), low=min(close - 0.3, 99.0))
    values = tuple([100.0] * 20)
    atr = tuple([2.0] * 20)
    indicators = IndicatorSet(values, atr, values, values, values, values, tuple(["flat"] * 20), ())
    return MarketAnalysis(
        bar=bars[-1], indicators=indicators, h1_absolute="flat", top_divergence=divergence,
        bottom_divergence=None, bearish_pattern=False, bullish_pattern=False,
        close_cross="none", macd_cross=macd_cross, m15_bars=tuple(bars),
        h1_top_divergence=h1_divergence, h1_top_pending=h1_pending,
    )


class TrendDirectionSemanticsTest(unittest.TestCase):
    def test_bull_p1_is_below_state_not_any_cross_event(self) -> None:
        result = trend_facts(analysis(close=99.0, macd_cross="up", divergence=None), TrendState(trend_state="bull", trend_extreme=110.0))
        self.assertTrue(result.p_close_cross)
        self.assertFalse(result.p_macd_cross)

    def test_bull_macd_p2_requires_down_cross_after_confirmed_n(self) -> None:
        divergence = Divergence("top", "B_hist", 3, 10, 12, 3, ("dif", "hist"), 200.0)
        result = trend_facts(analysis(close=99.0, macd_cross="down", divergence=divergence), TrendState(trend_state="bull", trend_extreme=110.0))
        self.assertTrue(result.top_divergence)
        self.assertTrue(result.p_macd_cross)
        self.assertTrue(result.strong_break_atr_met)

    def test_structure_reverse_requires_pattern(self) -> None:
        result = trend_facts(analysis(close=99.0, macd_cross="none", divergence=None), TrendState(trend_state="bull", trend_extreme=110.0))
        self.assertFalse(result.r_bearish_reverse)

    def test_flat_or_reverse_h1_unblocks_blocked_direction(self) -> None:
        result = trend_facts(analysis(close=99.0, macd_cross="none", divergence=None), TrendState(blocked_dir="bear"))
        self.assertTrue(result.unblock_allowed)

    def test_h1_divergence_reports_confirmed_or_pending_for_bull(self) -> None:
        confirmed = Divergence("top", "B_hist", 0, 1, 2, 3, ("dif", "hist"), 200.0)
        confirmed_facts = trend_facts(
            analysis(close=99.0, macd_cross="none", divergence=None, h1_divergence=confirmed),
            TrendState(trend_state="bull", trend_extreme=110.0),
        )
        pending_facts = trend_facts(
            analysis(close=99.0, macd_cross="none", divergence=None, h1_pending=True),
            TrendState(trend_state="bull", trend_extreme=110.0),
        )
        self.assertEqual(confirmed_facts.h1_divergence, "confirmed")
        self.assertEqual(pending_facts.h1_divergence, "pending")

    def test_s1_requires_extreme_then_directional_slope_to_flat(self) -> None:
        source = analysis(close=100.0, macd_cross="none", divergence=None)
        bars = list(source.m15_bars)
        bars[15] = replace(bars[15], high=110.0)
        slopes = ["flat"] * 20
        slopes[15], slopes[16] = "up", "flat"
        indicators = replace(source.indicators, slope_state=tuple(slopes))
        result = trend_facts(replace(source, indicators=indicators, m15_bars=tuple(bars)),
                             TrendState(trend_state="bull", trend_extreme=110.0))
        self.assertTrue(result.s1_extreme_then_flat)

    def test_s2_upper_pinbar_break_near_extreme_enables_structure_reverse(self) -> None:
        source = analysis(close=99.0, macd_cross="none", divergence=None)
        bars = list(source.m15_bars)
        bars[-2] = replace(bars[-2], open=100.0, close=100.0, high=103.0, low=99.8)
        bars[-1] = replace(bars[-1], open=99.9, close=99.5, high=100.0, low=99.2)
        result = trend_facts(replace(source, bar=bars[-1], m15_bars=tuple(bars)),
                             TrendState(trend_state="bull", trend_extreme=110.0))
        self.assertTrue(result.s2_bearish_pattern)
        self.assertTrue(result.r_bearish_reverse)


if __name__ == "__main__":
    unittest.main()
