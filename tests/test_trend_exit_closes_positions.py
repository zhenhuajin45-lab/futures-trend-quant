from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from gm.api import (
    MODE_BACKTEST,
    OrderStatus_Canceled,
    OrderStatus_Filled,
    PositionEffect_Open,
)

from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.execution import TradeRecord
from futures_trend_quant.core.pipeline import StrategyRuntimeBook
from futures_trend_quant.live.gm_strategy import _handle_trend_exit, on_order_status


def _record(evidence: str, status: str, order_id: str = "") -> TradeRecord:
    return TradeRecord(
        symbol="SHFE.AU2612",
        product="AU",
        cluster="precious_metals",
        direction="bull",
        volume=1,
        planned_entry=850,
        entry_price=850,
        risk_distance=2,
        stop=848,
        target=854,
        multiplier=1000,
        price_tick=0.02,
        evidence_id=evidence,
        opened_bar_id="b1",
        csymbol="SHFE.AU",
        status=status,
        open_order_id=order_id,
    )


def test_confirmed_trend_flip_closes_open_and_cancels_pending_open() -> None:
    open_record = _record("open-evidence", "open")
    pending = _record("pending-evidence", "pending_open", "open-order")

    class Context:
        mode = MODE_BACKTEST
        ftq_config = load_config(default_config_path())
        ftq_trades = {open_record.evidence_id: open_record, pending.evidence_id: pending}

    with (
        patch(
            "futures_trend_quant.live.gm_strategy.order_volume",
            return_value=[{"cl_ord_id": "close-order"}],
        ) as close_order,
        patch(
            "futures_trend_quant.live.gm_strategy.get_unfinished_orders",
            return_value=[{"cl_ord_id": "open-order", "account_id": "masked-test-account"}],
        ),
        patch("futures_trend_quant.live.gm_strategy.order_cancel") as cancel,
        patch("futures_trend_quant.live.gm_strategy._log"),
    ):
        _handle_trend_exit(Context(), "SHFE.AU2612", "bear")

    assert open_record.status == "pending_close"
    assert open_record.close_reason == "TREND_EXIT"
    assert pending.pending_exit_reason == "TREND_EXIT"
    close_order.assert_called_once()
    cancel.assert_called_once_with(
        [{"cl_ord_id": "open-order", "account_id": "masked-test-account"}]
    )


def test_partially_filled_canceled_open_is_immediately_closed_after_trend_exit() -> None:
    record = _record("partial-evidence", "pending_open", "open-order")
    record.pending_exit_reason = "TREND_EXIT"

    class Context:
        mode = MODE_BACKTEST
        ftq_config = load_config(default_config_path())
        ftq_trades = {record.evidence_id: record}
        ftq_runtime = StrategyRuntimeBook()
        ftq_product_realized = {}

    order = SimpleNamespace(
        symbol=record.symbol,
        status=OrderStatus_Canceled,
        position_effect=PositionEffect_Open,
        filled_volume=1,
        filled_vwap=850.1,
        cl_ord_id="open-order",
        ord_rej_reason_detail="",
    )
    with (
        patch(
            "futures_trend_quant.live.gm_strategy.order_volume",
            return_value=[{"cl_ord_id": "close-after-partial"}],
        ) as close_order,
        patch("futures_trend_quant.live.gm_strategy._log"),
    ):
        on_order_status(Context(), order)

    assert record.status == "pending_close"
    assert record.close_reason == "TREND_EXIT"
    assert record.evidence_id in Context.ftq_runtime.used_evidence_ids
    assert round(record.risk_distance, 6) == 2.1
    close_order.assert_called_once()


def test_open_fill_beyond_planned_stop_is_immediately_closed_without_negative_risk() -> None:
    record = _record("slipped-evidence", "pending_open", "open-order")

    class Context:
        mode = MODE_BACKTEST
        ftq_config = load_config(default_config_path())
        ftq_trades = {record.evidence_id: record}
        ftq_runtime = StrategyRuntimeBook()
        ftq_product_realized = {}

    order = SimpleNamespace(
        symbol=record.symbol,
        status=OrderStatus_Filled,
        position_effect=PositionEffect_Open,
        filled_volume=1,
        filled_vwap=847.9,
        cl_ord_id="open-order",
        ord_rej_reason_detail="",
    )
    with (
        patch(
            "futures_trend_quant.live.gm_strategy.order_volume",
            return_value=[{"cl_ord_id": "emergency-close"}],
        ) as close_order,
        patch("futures_trend_quant.live.gm_strategy._log"),
    ):
        on_order_status(Context(), order)

    assert record.status == "pending_close"
    assert record.close_reason == "FILL_BEYOND_STOP"
    assert record.risk_distance == 2
    assert record.target == 854
    close_order.assert_called_once()
