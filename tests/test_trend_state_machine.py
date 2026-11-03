from __future__ import annotations

import unittest

from futures_trend_quant.core.models import TrendState
from futures_trend_quant.core.trend.state_machine import TrendFacts, step_trend


def facts(bar_id: str, **overrides: object) -> TrendFacts:
    values: dict[str, object] = {
        "bar_id": bar_id,
        "close": 101.0,
        "high": 102.0,
        "low": 99.0,
        "close_side": "above",
        "slope_state": "up",
    }
    values.update(overrides)
    return TrendFacts(**values)  # type: ignore[arg-type]


class TrendStateMachineTest(unittest.TestCase):
    def test_none_requires_two_same_direction_votes(self) -> None:
        state = TrendState()
        first = step_trend(state, facts("b1"))
        self.assertFalse(first.confirmed)
        self.assertEqual(first.state.trend_state, "none")
        second = step_trend(first.state, facts("b2"))
        self.assertTrue(second.confirmed)
        self.assertEqual(second.state.trend_state, "bull")

    def test_direction_change_resets_establish_votes(self) -> None:
        first = step_trend(TrendState(), facts("b1"))
        bearish = facts("b2", close_side="below", slope_state="down", close=98.0, high=100.0, low=97.0)
        second = step_trend(first.state, bearish)
        self.assertFalse(second.confirmed)
        self.assertEqual(second.state.establish_direction, "bear")
        self.assertEqual(second.state.establish_votes, 1)

    def test_leave_votes_merge_and_latest_target_wins(self) -> None:
        state = TrendState(trend_state="bull", trend_extreme=110.0, since_bar_id="old")
        first = step_trend(
            state,
            facts("b1", top_divergence=True, s2_bearish_pattern=True, p_close_cross=True, h1_state="weakening"),
        )
        self.assertEqual(first.proposed_action, "end_to_none")
        second = step_trend(
            first.state,
            facts(
                "b2",
                top_divergence=True,
                s2_bearish_pattern=True,
                p_close_cross=True,
                h1_state="weakening",
                h1_divergence="confirmed",
            ),
        )
        self.assertTrue(second.confirmed)
        self.assertEqual(second.state.trend_state, "bear")

    def test_h1_strong_veto_resets_leave_votes(self) -> None:
        state = TrendState(trend_state="bull", trend_extreme=110.0, since_bar_id="old", leave_votes=1)
        result = step_trend(
            state,
            facts("b1", top_divergence=True, s2_bearish_pattern=True, p_close_cross=True, h1_state="strong"),
        )
        self.assertEqual(result.proposed_action, "veto")
        self.assertEqual(result.state.leave_votes, 0)
        self.assertEqual(result.state.trend_state, "bull")

    def test_strong_exit_blocks_reverse_direction(self) -> None:
        state = TrendState(trend_state="bull", trend_extreme=110.0, since_bar_id="old", leave_votes=1)
        result = step_trend(
            state,
            facts(
                "b1",
                top_divergence=True,
                s1_extreme_then_flat=True,
                s2_bearish_pattern=True,
                p_close_cross=True,
                h1_state="strong",
                strong_break_atr_met=True,
            ),
        )
        self.assertTrue(result.confirmed)
        self.assertEqual(result.state.trend_state, "none")
        self.assertEqual(result.state.blocked_dir, "bear")

    def test_duplicate_bar_is_rejected(self) -> None:
        state = TrendState(last_closed_bar_id="b1")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            step_trend(state, facts("b1"))


if __name__ == "__main__":
    unittest.main()
