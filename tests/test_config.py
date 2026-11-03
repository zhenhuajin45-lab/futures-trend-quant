from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from futures_trend_quant.config import ConfigError, default_config_path, load_config


class ConfigTest(unittest.TestCase):
    def test_expected_universe_and_fail_closed_execution(self) -> None:
        config = load_config(default_config_path())
        self.assertFalse(config.execute_enabled)
        self.assertEqual(config.risk_pct, 0.01)
        self.assertEqual(config.initial_cash, 1_000_000)
        self.assertEqual(config.subscription_count, 1200)
        self.assertEqual(
            [product.csymbol for product in config.products],
            [
                "DCE.LH",
                "DCE.M",
                "CZCE.CF",
                "GFEX.LC",
                "CZCE.SA",
                "DCE.JD",
                "DCE.V",
                "DCE.EG",
                "SHFE.AU",
                "SHFE.AG",
                "CZCE.CJ",
                "CZCE.SR",
            ],
        )

    def test_execute_enabled_is_rejected(self) -> None:
        raw = json.loads(default_config_path().read_text(encoding="utf-8"))
        raw["execution"]["execute_enabled"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "execute_enabled"):
                load_config(path)

    def test_entry_switches_and_fact_window_are_fail_closed(self) -> None:
        raw = json.loads(default_config_path().read_text(encoding="utf-8"))
        raw["data"]["subscription_count"] = 1199
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "1200"):
                load_config(path)

    def test_path_d_stack_cannot_exceed_product_cap(self) -> None:
        raw = json.loads(default_config_path().read_text(encoding="utf-8"))
        raw["risk"]["max_positions_per_product"] = 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "stack"):
                load_config(path)

    def test_rollover_and_position_management_enums_match_sprint_contract(self) -> None:
        raw = json.loads(default_config_path().read_text(encoding="utf-8"))
        raw["backtest"]["rollover_policy"] = "close_and_reopen"
        raw["rules"]["pm_mode"] = "dynamic"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "rollover_policy"):
                load_config(path)

        raw["backtest"]["rollover_policy"] = "hold"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "pm_mode"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
