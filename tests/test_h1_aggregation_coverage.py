from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import unittest

from futures_trend_quant.core.facts.pipeline import aggregate_m15_to_h1_with_coverage, trading_day_for_bar
from helpers import make_bars


class H1AggregationCoverageTest(unittest.TestCase):
    def test_fixed_session_buckets_cover_every_input_without_merging(self) -> None:
        start = datetime(2026, 11, 3, 9, 0)
        bars = []
        for index, source in enumerate(make_bars(11, step=0.0)):
            bob = start + timedelta(minutes=15 * index)
            bars.append(replace(source, bob=bob, eob=bob + timedelta(minutes=15)))
        result = aggregate_m15_to_h1_with_coverage(bars)
        self.assertEqual([len(item.source_bar_ids) for item in result], [4, 1, 1, 4, 1])
        self.assertTrue(all(len(x.source_bar_ids)<=4 for x in result))
        covered = [source for item in result for source in item.source_bar_ids]
        self.assertEqual(covered, [bar.identity for bar in bars])

    def test_night_session_is_assigned_to_next_trading_day(self) -> None:
        source = make_bars(1)[0]
        night = replace(source, bob=datetime(2026, 11, 6, 21, 0), eob=datetime(2026, 11, 6, 21, 15))
        self.assertEqual(trading_day_for_bar(night).isoformat(), "2026-11-09")


if __name__ == "__main__":
    unittest.main()
