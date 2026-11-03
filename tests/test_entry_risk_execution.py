from __future__ import annotations

from datetime import datetime
import unittest
from unittest.mock import patch

from gm.api import MODE_BACKTEST, OrderSide_Buy, OrderType_Market, PositionEffect_Open

from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.entry import EntryEvidence, evaluate_path_d
from futures_trend_quant.core.execution import TradeRecord, exit_reason
from futures_trend_quant.core.models import TrendState
from futures_trend_quant.core.risk import ContractSpec, build_stop_plan, calculate_size
from futures_trend_quant.live.gm_strategy import _process_m15, _submit_open
from helpers import make_bars


class EntryRiskExecutionTest(unittest.TestCase):
    def test_stop_and_one_percent_sizing(self) -> None:
        spec = ContractSpec("SHFE.AU2612", price_tick=0.02, multiplier=1000, margin_ratio=0.12)
        plan = build_stop_plan(direction="bull", entry=850, anchor=848, atr=1.0, tick=0.02)
        self.assertLess(plan.stop, plan.entry)
        self.assertAlmostEqual(plan.target - plan.entry, 2 * (plan.entry - plan.stop))
        size = calculate_size(
            equity=10_000_000,
            available=10_000_000,
            entry=plan.entry,
            stop_distance=plan.distance,
            spec=spec,
            risk_pct=0.01,
            risk_cap_abs=None,
            margin_usage_pct=0.8,
        )
        self.assertEqual(size.volume, min(size.risk_volume, size.margin_volume))
        self.assertLessEqual(size.volume * size.risk_per_contract, size.risk_budget)

    def test_stop_wins_if_same_bar_touches_stop_and_target(self) -> None:
        record = TradeRecord(
            symbol="DCE.M2605", product="M", cluster="oilseed", direction="bull", volume=1,
            planned_entry=100, entry_price=100, risk_distance=2, stop=98, target=104,
            multiplier=10, price_tick=1, evidence_id="e1", opened_bar_id="b1", status="open",
        )
        self.assertEqual(exit_reason(record, high=105, low=97), "STOP")

    def test_path_d_rejects_h1_and_accepts_complete_chain(self) -> None:
        bars = make_bars(60)
        rejected = evaluate_path_d(
            direction="bull", bar_id="b1", h1_state="flat", m15_slope_state="up",
            reverse_divergence=False, m5_bars=bars, m1_bars=bars,
        )
        self.assertEqual(rejected.reason, "H1_NOT_STRONG")
        with (
            patch("futures_trend_quant.core.entry._recent_m5_event", return_value="ema_cross_up:e1"),
            patch("futures_trend_quant.core.entry._m1_strong_reverse", return_value=False),
        ):
            accepted = evaluate_path_d(
                direction="bull", bar_id="b1", h1_state="strong", m15_slope_state="up",
                reverse_divergence=False, m5_bars=bars, m1_bars=bars,
            )
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.reason, "PATH_D_ACCEPTED")

    def test_closed_m15_reaches_final_gm_order_call_in_backtest_only(self) -> None:
        symbol = "SHFE.AU2612"
        config = load_config(default_config_path())

        class Context:
            mode = MODE_BACKTEST
            now = datetime(2026, 10, 20, 10, 0)
            ftq_config = config
            ftq_states = {
                symbol: TrendState(establish_votes=1, establish_direction="bull", last_closed_bar_id="prior")
            }
            ftq_specs = {symbol: ContractSpec(symbol, 0.02, 1000, 0.12)}
            ftq_product_by_symbol = {symbol: config.products[-2]}
            ftq_trades = {}
            ftq_used_evidence = set()
            ftq_trade_date = ""
            ftq_day_start_nav = 0.0
            ftq_product_realized = {}

        bars = make_bars(500, symbol=symbol)
        accepted = EntryEvidence(True, "bull", "path_d|bull|e2e", "PATH_D_ACCEPTED", "ema_cross_up:e1", False)
        with (
            patch("futures_trend_quant.live.gm_strategy._window", return_value=bars),
            patch("futures_trend_quant.live.gm_strategy.evaluate_path_d", return_value=accepted),
            patch("futures_trend_quant.live.gm_strategy._account_snapshot", return_value=(10_000_000, 10_000_000)),
            patch("futures_trend_quant.live.gm_strategy._effective_trade_date", return_value="2026-10-20"),
            patch("futures_trend_quant.live.gm_strategy.get_position", return_value=[]),
            patch("futures_trend_quant.live.gm_strategy.order_volume", return_value=[{"cl_ord_id": "order-1"}]) as submit,
            patch("futures_trend_quant.live.gm_strategy._log"),
        ):
            _process_m15(Context(), symbol)
        submit.assert_called_once()
        self.assertEqual(
            {key: submit.call_args.kwargs[key] for key in ("symbol", "side", "order_type", "position_effect")},
            {
                "symbol": symbol,
                "side": OrderSide_Buy,
                "order_type": OrderType_Market,
                "position_effect": PositionEffect_Open,
            },
        )

    def test_submit_open_cannot_bypass_non_backtest_gate(self) -> None:
        config = load_config(default_config_path())
        record = TradeRecord(
            symbol="DCE.M2605", product="M", cluster="oilseed", direction="bull", volume=1,
            planned_entry=100, entry_price=100, risk_distance=2, stop=98, target=104,
            multiplier=10, price_tick=1, evidence_id="e1", opened_bar_id="b1",
        )

        class Context:
            mode = 1
            ftq_config = config
            ftq_trades = {record.symbol: record}

        with (
            patch("futures_trend_quant.live.gm_strategy.order_volume") as submit,
            patch("futures_trend_quant.live.gm_strategy._log"),
        ):
            _submit_open(Context(), record)
        submit.assert_not_called()
        self.assertNotIn(record.symbol, Context.ftq_trades)
