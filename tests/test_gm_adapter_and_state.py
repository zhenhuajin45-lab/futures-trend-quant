from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

from gm.api import MODE_BACKTEST

from futures_trend_quant.config import Product
from futures_trend_quant.core.models import TrendState
from futures_trend_quant.core.state_store import StateStore
from futures_trend_quant.data.gm_contracts import (
    ContractResolutionError,
    diagnose_contract_history,
    resolve_main_contracts,
)
from futures_trend_quant.live.gm_strategy import (
    _can_place_orders,
    _rolled_csymbols,
    _sanitize_log_value,
    on_bar,
)


class GmAdapterAndStateTest(unittest.TestCase):
    def test_on_bar_uses_gm_subscript_contract_not_dict_conversion(self) -> None:
        class GmLikeBar:
            keys = None

            def __init__(self) -> None:
                self.values = {"symbol": "SHFE.AU2612", "frequency": "60s"}

            def __getitem__(self, key: str):
                return self.values[key]

        class Config:
            frequencies = ("60s", "300s", "900s")
            subscription_count = 320

        class Context:
            ftq_symbols = frozenset({"SHFE.AU2612"})
            ftq_trades = {}
            ftq_config = Config()

            def data(self, **_):
                return None

        with patch("futures_trend_quant.live.gm_strategy._log") as log:
            on_bar(Context(), [GmLikeBar()])
        log.assert_not_called()

    def test_contract_resolution_requires_one_mapping_per_product(self) -> None:
        products = (
            Product("沪金", "SHFE", "AU", "SHFE.AU", "precious_metals"),
            Product("沪银", "SHFE", "AG", "SHFE.AG", "precious_metals"),
        )

        def fetch(*, csymbol: str, start_date: str, end_date: str):
            return [{"trade_date": start_date, "symbol": csymbol + "2606"}]

        contracts = resolve_main_contracts(products, "2026-11-03", fetch)
        self.assertEqual([contract.symbol for contract in contracts], ["SHFE.AU2606", "SHFE.AG2606"])

    def test_contract_resolution_fails_closed_on_missing_product(self) -> None:
        products = (Product("沪金", "SHFE", "AU", "SHFE.AU", "precious_metals"),)
        with self.assertRaisesRegex(
            ContractResolutionError,
            r"SHFE\.AU:mapping_count=0:row_count=0:dates=\[\]:symbols=\[\]",
        ):
            resolve_main_contracts(products, "2026-11-03", lambda **_: [])

    def test_contract_resolution_reports_returned_date_mismatch(self) -> None:
        products = (Product("沪金", "SHFE", "AU", "SHFE.AU", "precious_metals"),)
        with self.assertRaisesRegex(
            ContractResolutionError,
            r"mapping_count=0:row_count=1:dates=\['2026-11-02'\]:symbols=\['SHFE\.AU2612'\]",
        ):
            resolve_main_contracts(
                products,
                "2026-11-03",
                lambda **_: [{"trade_date": "2026-11-02", "symbol": "SHFE.AU2612"}],
            )

    def test_contract_history_diagnostic_is_bounded(self) -> None:
        products = (Product("沪金", "SHFE", "AU", "SHFE.AU", "precious_metals"),)

        def fetch(**_):
            return [
                {"trade_date": f"2026-10-{day:02d}", "symbol": "SHFE.AU2612"}
                for day in range(16, 31)
            ]

        diagnostics = diagnose_contract_history(products, "2026-11-03", fetch)
        self.assertEqual(diagnostics["SHFE.AU"]["row_count"], 15)
        self.assertEqual(len(diagnostics["SHFE.AU"]["tail"]), 3)
        self.assertEqual(diagnostics["SHFE.AU"]["start_date"], "2026-10-20")

    def test_state_roundtrip_and_schema_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(directory)
            state = TrendState(trend_state="bull", since_bar_id="b1", trend_extreme=101.2)
            path = store.save("SHFE.AU2606", state, evidence={"bar_id": "b1"})
            self.assertEqual(store.load("SHFE.AU2606"), state)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["state"]["unknown"] = 1
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown"):
                store.load("SHFE.AU2606")

    def test_order_gate_requires_platform_backtest_and_config_flag(self) -> None:
        class Config:
            execution_mode = "backtest"
            backtest_orders_enabled = True
            execute_enabled = False

        class Context:
            mode = MODE_BACKTEST
            ftq_config = Config()

        self.assertTrue(_can_place_orders(Context()))
        Context.mode = 1
        self.assertFalse(_can_place_orders(Context()))
        Context.mode = MODE_BACKTEST
        Config.backtest_orders_enabled = False
        self.assertFalse(_can_place_orders(Context()))

    def test_rollover_detection_only_blocks_changed_existing_csymbols(self) -> None:
        previous = {"SHFE.AU": "SHFE.au2612", "DCE.M": "DCE.m2701"}
        current = {
            "SHFE.AU": "SHFE.au2702",
            "DCE.M": "DCE.m2701",
            "SHFE.AG": "SHFE.ag2612",
        }
        self.assertEqual(_rolled_csymbols(previous, current), {"SHFE.AU"})

    def test_log_sanitizer_removes_account_and_token_fields_recursively(self) -> None:
        public = _sanitize_log_value(
            {"account_id": "secret", "result": {"token": "secret", "pnl": 1.2}}
        )
        self.assertEqual(public, {"result": {"pnl": 1.2}})


if __name__ == "__main__":
    unittest.main()
