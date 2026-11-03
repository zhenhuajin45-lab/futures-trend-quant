from __future__ import annotations

import json
from pathlib import Path
import unittest


class DatasetConsistencyTest(unittest.TestCase):
    def test_reference_ema_dea_and_hist_recurrences(self) -> None:
        path = Path(__file__).parents[1] / "data" / "reference" / "dataset.json"
        dataset = json.loads(path.read_text(encoding="utf-8"))
        ema_alpha, dea_alpha = 2.0 / 35.0, 2.0 / 10.0
        fast_alpha, slow_alpha = 2.0 / 13.0, 2.0 / 27.0
        checked = 0
        for segment in dataset["segments"]:
            bars = segment["bars"]
            # Infer the hidden EMA26 seed from the first DIF transition, then
            # verify the 12/26 EMA recurrence. This does not need the omitted
            # pre-segment source bars and still detects a mismatched DIF series.
            ema26 = (
                bars[1]["dif"]
                - (fast_alpha - slow_alpha) * bars[1]["c"]
                - (1 - fast_alpha) * bars[0]["dif"]
            ) / ((1 - fast_alpha) - (1 - slow_alpha))
            for previous, current in zip(bars, bars[1:]):
                # Dataset OHLC is rounded to 0.01 while the reference EMA was
                # calculated from the unrounded feed. The tight feasible bound
                # is therefore alpha * half a tick, not 1e-6.
                self.assertAlmostEqual(current["ema34"], ema_alpha * current["c"] + (1 - ema_alpha) * previous["ema34"], delta=3e-4)
                self.assertAlmostEqual(current["dea"], dea_alpha * current["dif"] + (1 - dea_alpha) * previous["dea"], delta=1e-6)
                self.assertAlmostEqual(current["hist"], current["dif"] - current["dea"], delta=1e-6)
                ema12 = ema26 + previous["dif"]
                next_ema12 = fast_alpha * current["c"] + (1 - fast_alpha) * ema12
                next_ema26 = slow_alpha * current["c"] + (1 - slow_alpha) * ema26
                self.assertAlmostEqual(current["dif"], next_ema12 - next_ema26, delta=1e-6)
                ema26 = next_ema26
                checked += 1
        self.assertGreater(checked, 1500)


if __name__ == "__main__":
    unittest.main()
