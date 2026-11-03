from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest

from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.facts.pipeline import analyze_market, trend_facts
from futures_trend_quant.core.models import Bar, TrendState
from futures_trend_quant.core.trend.state_machine import step_trend


def _replay(segment_id: str) -> list[tuple[str, str]]:
    source = Path(__file__).parents[1] / "data" / "reference" / "dataset.json"
    dataset = json.loads(source.read_text(encoding="utf-8"))
    segment = next(item for item in dataset["segments"] if item["id"] == segment_id)
    rules = load_config(default_config_path()).raw["rules"]
    bars: list[Bar] = []
    state = TrendState()
    result: list[tuple[str, str]] = []
    for raw in segment["bars"]:
        bob = datetime.strptime(raw["mt5_label"][:19], "%Y-%m-%d %H:%M:%S")
        bars.append(
            Bar(
                symbol="XAUUSD",
                frequency="900s",
                bob=bob,
                eob=bob + timedelta(minutes=15),
                open=raw["o"],
                high=raw["h"],
                low=raw["l"],
                close=raw["c"],
            )
        )
        try:
            analysis = analyze_market(bars, rules)
        except ValueError:
            continue
        decision = step_trend(state, trend_facts(analysis, state))
        state = decision.state
        result.append((raw["mt5_label"][:16], state.trend_state))
    return result


class XauusdTrendAnchorTest(unittest.TestCase):
    """Known-red T2 anchors retained as executable acceptance evidence.

    The supplied segments contain only 96--110 background M15 bars, while the
    corrected H1 EMA path needs at least 47 H1 aggregates (about 188 M15 bars).
    These expected failures must not be converted into passes by weakening the
    warmup. They become ordinary assertions when a longer source export exists.
    """

    @unittest.expectedFailure
    def test_c1_front_turns_bull_no_later_than_2026_10_26_1930(self) -> None:
        states = _replay("c1-front")
        target = [state for stamp, state in states if stamp <= "2026-10-26 19:30"]
        self.assertIn("bull", target)

    @unittest.expectedFailure
    def test_s1027_never_bear_between_1315_and_1930(self) -> None:
        states = _replay("s1027")
        target = [state for stamp, state in states if "2026-10-27 13:15" <= stamp <= "2026-10-27 19:30"]
        self.assertTrue(target, "corrected H1 warmup produced no facts in the target window")
        self.assertNotIn("bear", target)

    @unittest.expectedFailure
    def test_s1030_is_bear_throughout_0145_to_1145(self) -> None:
        states = _replay("s1030")
        target = [state for stamp, state in states if "2026-10-30 01:45" <= stamp <= "2026-10-30 11:45"]
        self.assertTrue(target, "corrected H1 warmup produced no facts in the target window")
        self.assertTrue(all(state == "bear" for state in target))


if __name__ == "__main__":
    unittest.main()
