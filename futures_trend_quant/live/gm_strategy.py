from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping

from gm.api import (  # type: ignore
    MODE_BACKTEST,
    OrderSide_Buy,
    OrderSide_Sell,
    OrderStatus_Canceled,
    OrderStatus_Filled,
    OrderStatus_Rejected,
    OrderType_Market,
    PositionEffect_Close,
    PositionEffect_Open,
    fut_get_continuous_contracts,
    get_cash,
    get_instrumentinfos,
    get_next_n_trading_dates,
    get_position,
    order_volume,
    schedule,
    subscribe,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from futures_trend_quant.config import Product, default_config_path, load_config
from futures_trend_quant.core.entry import evaluate_path_d
from futures_trend_quant.core.execution import TradeRecord, exit_reason
from futures_trend_quant.core.facts.pipeline import analyze_market, trend_facts
from futures_trend_quant.core.models import Bar, TrendState
from futures_trend_quant.core.risk import ContractSpec, build_stop_plan, calculate_size
from futures_trend_quant.core.trend.state_machine import step_trend
from futures_trend_quant.data.gm_bars import bars_from_records
from futures_trend_quant.data.gm_contracts import ContractResolutionError, diagnose_contract_history, resolve_main_contracts


BAR_FIELDS = "symbol,frequency,open,high,low,close,volume,amount,position,bob,eob,pre_close"


def _log(tag: str, **payload: Any) -> None:
    print(f"[{tag}] {json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)}")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return value[name]
    except (KeyError, TypeError, AttributeError):
        return getattr(value, name, default)


def _sanitize_log_value(value: Any) -> Any:
    sensitive = {"account_id", "account_name", "channel_id", "token"}
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_log_value(item)
            for key, item in value.items()
            if str(key).lower() not in sensitive
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_log_value(item) for item in value]
    return value


def _effective_trade_date(now: datetime) -> str:
    today = now.strftime("%Y-%m-%d")
    if now.hour <= 15:
        return today
    dates = get_next_n_trading_dates(exchange="SHSE", date=today, n=1)
    if not dates:
        raise ContractResolutionError(f"no next trading date after {today}")
    return str(dates[0])[:10]


def _can_place_orders(context: Any) -> bool:
    config = context.ftq_config
    return bool(
        getattr(context, "mode", None) == MODE_BACKTEST
        and config.execution_mode == "backtest"
        and config.backtest_orders_enabled
        and not config.execute_enabled
    )


def _refresh_specs(context: Any, symbols: list[str]) -> None:
    rows = get_instrumentinfos(
        symbols=",".join(symbols),
        fields="symbol,price_tick,multiplier,margin_ratio",
        df=False,
    )
    specs: dict[str, ContractSpec] = {}
    for row in rows or []:
        symbol = str(_field(row, "symbol", ""))
        if symbol not in symbols:
            continue
        try:
            specs[symbol] = ContractSpec(
                symbol=symbol,
                price_tick=float(_field(row, "price_tick")),
                multiplier=float(_field(row, "multiplier")),
                margin_ratio=float(_field(row, "margin_ratio")),
            )
        except (TypeError, ValueError):
            continue
    missing = sorted(set(symbols) - set(specs))
    if missing:
        raise RuntimeError(f"missing contract specifications: {missing}")
    context.ftq_specs.update(specs)


def _refresh_contracts(context: Any) -> None:
    config = context.ftq_config
    trade_date = _effective_trade_date(context.now)
    try:
        contracts = resolve_main_contracts(config.products, trade_date, fut_get_continuous_contracts)
    except ContractResolutionError:
        _log(
            "CONTRACT_DIAG",
            trade_date=trade_date,
            history=diagnose_contract_history(config.products, trade_date, fut_get_continuous_contracts),
        )
        raise
    new_map = {contract.csymbol: contract.symbol for contract in contracts}
    symbols = list(new_map.values())
    if len(set(symbols)) != len(config.products):
        raise ContractResolutionError("resolved main contracts are not unique")
    changed = new_map != getattr(context, "ftq_contracts", {})
    product_by_csymbol = {product.csymbol: product for product in config.products}
    context.ftq_product_by_symbol.update({symbol: product_by_csymbol[csymbol] for csymbol, symbol in new_map.items()})
    context.ftq_contracts = new_map
    active_symbols = list(context.ftq_trades)
    subscribed_symbols = list(dict.fromkeys(symbols + active_symbols))
    context.ftq_symbols = frozenset(subscribed_symbols)
    _refresh_specs(context, subscribed_symbols)
    for index, frequency in enumerate(config.frequencies):
        subscribe(
            symbols=subscribed_symbols,
            frequency=frequency,
            count=config.subscription_count,
            wait_group=False,
            unsubscribe_previous=(index == 0),
            fields=BAR_FIELDS,
            format="df",
        )
    _log("CONTRACT_REFRESH", trade_date=trade_date, changed=changed, mappings=new_map, retained=active_symbols)


def init(context: Any) -> None:
    config = load_config(default_config_path())
    context.ftq_config = config
    context.ftq_contracts = {}
    context.ftq_symbols = frozenset()
    context.ftq_specs: dict[str, ContractSpec] = {}
    context.ftq_product_by_symbol: dict[str, Product] = {}
    context.ftq_states: dict[str, TrendState] = {}
    context.ftq_trades: dict[str, TradeRecord] = {}
    context.ftq_used_evidence: set[str] = set()
    context.ftq_trade_date = ""
    context.ftq_day_start_nav = 0.0
    context.ftq_product_realized: dict[str, float] = {}
    _refresh_contracts(context)
    schedule(schedule_func=_refresh_contracts, date_rule="1d", time_rule="08:55:00")
    schedule(schedule_func=_refresh_contracts, date_rule="1d", time_rule="20:55:00")
    _log(
        "INIT",
        strategy=config.raw["strategy_name"],
        platform_mode=getattr(context, "mode", None),
        execution_mode=config.execution_mode,
        backtest_orders_enabled=config.backtest_orders_enabled,
        live_orders_enabled=config.execute_enabled,
        order_gate=_can_place_orders(context),
        products=[product.csymbol for product in config.products],
    )


def _records_from_dataframe(frame: Any) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    return frame.to_dict("records")


def _window(context: Any, symbol: str, frequency: str, count: int | None = None) -> list[Bar]:
    frame = context.data(
        symbol=symbol,
        frequency=frequency,
        count=count or context.ftq_config.subscription_count,
        fields=BAR_FIELDS,
    )
    series = bars_from_records(_records_from_dataframe(frame))
    if not series:
        raise ValueError("empty data window")
    return series


def _raw_bar(raw: Any) -> Bar:
    fields = ("symbol", "frequency", "bob", "eob", "open", "high", "low", "close", "volume", "amount", "position", "pre_close")
    return bars_from_records([{field: _field(raw, field) for field in fields}])[0]


def _account_snapshot() -> tuple[float, float]:
    cash = get_cash()
    if isinstance(cash, (list, tuple)):
        if not cash:
            raise RuntimeError("empty cash snapshot")
        cash = cash[0]
    nav = float(_field(cash, "nav", 0) or 0)
    available = float(_field(cash, "available", 0) or 0)
    if not all(math.isfinite(value) and value > 0 for value in (nav, available)):
        raise RuntimeError("invalid cash snapshot")
    return nav, available


def _reset_day_if_needed(context: Any, nav: float) -> None:
    trade_date = _effective_trade_date(context.now)
    if trade_date == context.ftq_trade_date:
        return
    context.ftq_trade_date = trade_date
    context.ftq_day_start_nav = nav
    context.ftq_product_realized = {}
    _log("RISK_DAY_RESET", trade_date=trade_date, start_nav=nav)


def _broker_position_symbols() -> set[str]:
    rows = get_position() or []
    if not isinstance(rows, (list, tuple)):
        rows = [rows]
    return {
        str(_field(row, "symbol"))
        for row in rows
        if int(_field(row, "volume", 0) or 0) > 0
    }


def _entry_guard(context: Any, symbol: str, product: Product, nav: float) -> tuple[bool, str]:
    config = context.ftq_config.raw["risk"]
    if symbol in context.ftq_trades:
        return False, "LOCAL_POSITION_OR_PENDING"
    broker_symbols = _broker_position_symbols()
    if symbol in broker_symbols:
        return False, "BROKER_POSITION_EXISTS"
    trades = list(context.ftq_trades.values())
    if len(trades) >= int(config["max_open_positions"]):
        return False, "PORTFOLIO_POSITION_CAP"
    if sum(item.product == product.product for item in trades) >= int(config["max_positions_per_product"]):
        return False, "PRODUCT_POSITION_CAP"
    if sum(item.cluster == product.cluster for item in trades) >= int(config["max_positions_per_cluster"]):
        return False, "CLUSTER_POSITION_CAP"
    portfolio_open_risk = sum(item.open_risk for item in trades)
    portfolio_pressure = max(0.0, context.ftq_day_start_nav - nav) + portfolio_open_risk
    if portfolio_pressure >= context.ftq_day_start_nav * float(config["portfolio_daily_loss_pct"]):
        return False, "PORTFOLIO_DAILY_LOSS_CAP"
    product_pressure = max(0.0, -context.ftq_product_realized.get(product.product, 0.0)) + sum(
        item.open_risk for item in trades if item.product == product.product
    )
    if product_pressure >= context.ftq_day_start_nav * float(config["product_daily_loss_pct"]):
        return False, "PRODUCT_DAILY_LOSS_CAP"
    return True, "ACCEPTED"


def _submit_open(context: Any, record: TradeRecord) -> None:
    if not _can_place_orders(context):
        _log("ORDER_BLOCKED", symbol=record.symbol, reason="BACKTEST_GATE_CLOSED")
        context.ftq_trades.pop(record.symbol, None)
        return
    side = OrderSide_Buy if record.direction == "bull" else OrderSide_Sell
    _log(
        "ORDER_INTENT", symbol=record.symbol, effect="OPEN", side=record.direction, volume=record.volume,
        entry=record.planned_entry, stop=record.stop, target=record.target, evidence_id=record.evidence_id,
        mode=getattr(context, "mode", None),
    )
    orders = order_volume(
        symbol=record.symbol,
        volume=record.volume,
        side=side,
        order_type=OrderType_Market,
        position_effect=PositionEffect_Open,
        price=0,
    )
    if not orders:
        context.ftq_trades.pop(record.symbol, None)
        raise RuntimeError("order_volume returned no open order")
    record.open_order_id = str(_field(orders[0], "cl_ord_id", ""))
    _log("ORDER_SUBMIT", symbol=record.symbol, effect="OPEN", cl_ord_id=record.open_order_id, volume=record.volume)


def _submit_close(context: Any, record: TradeRecord, reason: str) -> None:
    if record.status != "open":
        return
    if not _can_place_orders(context):
        _log("ORDER_BLOCKED", symbol=record.symbol, reason="BACKTEST_GATE_CLOSED", effect="CLOSE")
        return
    record.status = "pending_close"
    record.close_reason = reason
    side = OrderSide_Sell if record.direction == "bull" else OrderSide_Buy
    _log("EXIT_GUARD", symbol=record.symbol, reason=reason, volume=record.volume, stop=record.stop, target=record.target)
    orders = order_volume(
        symbol=record.symbol,
        volume=record.volume,
        side=side,
        order_type=OrderType_Market,
        position_effect=PositionEffect_Close,
        price=0,
    )
    if not orders:
        record.status = "open"
        raise RuntimeError("order_volume returned no close order")
    record.close_order_id = str(_field(orders[0], "cl_ord_id", ""))
    _log("ORDER_SUBMIT", symbol=record.symbol, effect="CLOSE", cl_ord_id=record.close_order_id, volume=record.volume)


def _handle_m1_exit(context: Any, raw: Any) -> None:
    symbol = str(_field(raw, "symbol", ""))
    record = context.ftq_trades.get(symbol)
    if record is None or record.status != "open":
        return
    bar = _raw_bar(raw)
    reason = exit_reason(record, high=bar.high, low=bar.low)
    if reason:
        _submit_close(context, record, reason)


def _process_m15(context: Any, symbol: str) -> None:
    config = context.ftq_config
    try:
        m15 = _window(context, symbol, "900s")
        m5 = _window(context, symbol, "300s")
        m1 = _window(context, symbol, "60s")
        analysis = analyze_market(m15, config.raw["rules"])
    except Exception as exc:
        _log("M15_FACTS_REJECT", symbol=symbol, error=type(exc).__name__, detail=str(exc)[:160])
        return
    state = context.ftq_states.get(symbol, TrendState())
    facts = trend_facts(analysis, state)
    try:
        decision = step_trend(state, facts, veto_enabled=bool(config.raw["rules"]["h1_veto_enabled"]))
    except ValueError as exc:
        _log("TREND_REJECT", symbol=symbol, bar_id=facts.bar_id, error=str(exc))
        return
    context.ftq_states[symbol] = decision.state
    index = len(analysis.indicators.ema34) - 1
    _log(
        "M15_FACTS", symbol=symbol, bar_id=facts.bar_id, close=facts.close,
        ema34=analysis.indicators.ema34[index], atr14=analysis.indicators.atr14[index],
        slope_state=facts.slope_state, h1_absolute=analysis.h1_absolute, h1_relative=facts.h1_state,
        top_divergence=facts.top_divergence, bottom_divergence=facts.bottom_divergence,
        trend_decision=decision.audit(),
    )
    if decision.state.trend_state not in ("bull", "bear"):
        return
    direction = decision.state.trend_state
    reverse_divergence = facts.top_divergence if direction == "bull" else facts.bottom_divergence
    evidence = evaluate_path_d(
        direction=direction,
        bar_id=facts.bar_id,
        h1_state=facts.h1_state,
        m15_slope_state=facts.slope_state,
        reverse_divergence=reverse_divergence,
        m5_bars=m5,
        m1_bars=m1,
    )
    _log("SIGNAL", symbol=symbol, **evidence.__dict__)
    if not evidence.accepted or evidence.evidence_id in context.ftq_used_evidence:
        return
    nav, available = _account_snapshot()
    _reset_day_if_needed(context, nav)
    product = context.ftq_product_by_symbol[symbol]
    accepted, reason = _entry_guard(context, symbol, product, nav)
    _log("ENTRY_GUARD", symbol=symbol, accepted=accepted, reason=reason, nav=nav, available=available)
    if not accepted:
        return
    atr_value = analysis.indicators.atr14[index]
    if atr_value is None:
        return
    anchor = min(bar.low for bar in m15[-20:]) if direction == "bull" else max(bar.high for bar in m15[-20:])
    spec = context.ftq_specs[symbol]
    try:
        stop_plan = build_stop_plan(
            direction=direction,
            entry=analysis.bar.close,
            anchor=anchor,
            atr=atr_value,
            tick=spec.price_tick,
            buffer_atr=float(config.raw["rules"]["stop_buffer_atr"]),
            min_atr=float(config.raw["rules"]["stop_min_atr"]),
            max_atr=float(config.raw["rules"]["stop_max_atr"]),
        )
        risk_config = config.raw["risk"]
        execution_cushion = max(
            3 * spec.price_tick,
            analysis.bar.close * float(risk_config["market_gap_buffer_bps"]) / 10_000,
        )
        sizing_distance = stop_plan.distance + execution_cushion
        size = calculate_size(
            equity=nav,
            available=available,
            entry=analysis.bar.close,
            stop_distance=sizing_distance,
            spec=spec,
            risk_pct=float(risk_config["risk_pct"]),
            risk_cap_abs=None if risk_config["risk_cap_abs"] is None else float(risk_config["risk_cap_abs"]),
            margin_usage_pct=float(risk_config["margin_usage_pct"]),
        )
    except ValueError as exc:
        _log("RISK_REJECT", symbol=symbol, reason=str(exc))
        return
    _log(
        "RISK_SIZE", symbol=symbol, **size.__dict__, structural_distance=stop_plan.distance,
        execution_cushion=execution_cushion, stop=stop_plan.stop, target=stop_plan.target,
    )
    if size.volume <= 0:
        _log("RISK_REJECT", symbol=symbol, reason="ZERO_VOLUME")
        return
    record = TradeRecord(
        symbol=symbol,
        product=product.product,
        cluster=product.cluster,
        direction=direction,
        volume=size.volume,
        planned_entry=stop_plan.entry,
        entry_price=stop_plan.entry,
        risk_distance=stop_plan.distance,
        stop=stop_plan.stop,
        target=stop_plan.target,
        multiplier=spec.multiplier,
        price_tick=spec.price_tick,
        evidence_id=evidence.evidence_id,
        opened_bar_id=facts.bar_id,
    )
    context.ftq_trades[symbol] = record
    context.ftq_used_evidence.add(evidence.evidence_id)
    _submit_open(context, record)


def on_bar(context: Any, bars: list[Any]) -> None:
    if not bars:
        _log("BAR_REJECT", reason="empty_callback")
        return
    for raw in bars:
        symbol = str(_field(raw, "symbol", ""))
        frequency = str(_field(raw, "frequency", ""))
        if not symbol or not frequency:
            _log("BAR_REJECT", reason="missing_identity_fields")
            continue
        if symbol not in context.ftq_symbols or frequency not in context.ftq_config.frequencies:
            _log("BAR_REJECT", reason="out_of_scope", symbol=symbol, frequency=frequency)
            continue
        if frequency == "60s":
            try:
                _handle_m1_exit(context, raw)
            except Exception as exc:
                _log("EXIT_REJECT", symbol=symbol, error=type(exc).__name__, detail=str(exc)[:160])
        elif frequency == "900s":
            _process_m15(context, symbol)


def on_order_status(context: Any, order: Any) -> None:
    symbol = str(_field(order, "symbol", ""))
    record = context.ftq_trades.get(symbol)
    status = int(_field(order, "status", 0) or 0)
    effect = int(_field(order, "position_effect", 0) or 0)
    filled_volume = int(_field(order, "filled_volume", 0) or 0)
    filled_vwap = float(_field(order, "filled_vwap", 0) or 0)
    cl_ord_id = str(_field(order, "cl_ord_id", ""))
    _log(
        "ORDER_STATUS", symbol=symbol, cl_ord_id=cl_ord_id, status=status, effect=effect,
        filled_volume=filled_volume, filled_vwap=filled_vwap,
        reject_reason=_field(order, "ord_rej_reason_detail", ""),
    )
    if record is None:
        _log("ORDER_STATUS_ORPHAN", symbol=symbol, cl_ord_id=cl_ord_id)
        return
    terminal_failure = status in (OrderStatus_Rejected, OrderStatus_Canceled)
    if effect == PositionEffect_Open:
        if status == OrderStatus_Filled or terminal_failure and filled_volume > 0:
            record.volume = filled_volume or record.volume
            if filled_vwap > 0:
                record.entry_price = filled_vwap
                if record.direction == "bull":
                    distance = filled_vwap - record.stop
                    record.target = math.ceil((filled_vwap + 2 * distance) / record.price_tick - 1e-12) * record.price_tick
                else:
                    distance = record.stop - filled_vwap
                    record.target = math.floor((filled_vwap - 2 * distance) / record.price_tick + 1e-12) * record.price_tick
            record.status = "open"
        elif terminal_failure:
            context.ftq_trades.pop(symbol, None)
    elif effect == PositionEffect_Close:
        if status == OrderStatus_Filled or terminal_failure and filled_volume >= record.volume:
            close_price = filled_vwap or record.entry_price
            signed_move = close_price - record.entry_price if record.direction == "bull" else record.entry_price - close_price
            pnl = signed_move * record.volume * record.multiplier
            context.ftq_product_realized[record.product] = context.ftq_product_realized.get(record.product, 0.0) + pnl
            _log("TRADE_CLOSED", symbol=symbol, reason=record.close_reason, pnl=pnl, close_price=close_price)
            context.ftq_trades.pop(symbol, None)
        elif terminal_failure:
            if filled_volume > 0:
                record.volume = max(0, record.volume - filled_volume)
            record.status = "open"


def on_backtest_finished(context: Any, indicator: Any) -> None:
    public_indicator = _sanitize_log_value(
        dict(indicator) if isinstance(indicator, Mapping) else str(indicator)
    )
    _log(
        "BACKTEST_FINISHED",
        indicator=public_indicator,
        active_trades={symbol: record.__dict__ for symbol, record in context.ftq_trades.items()},
        realized_by_product=context.ftq_product_realized,
        used_evidence=len(context.ftq_used_evidence),
    )


def on_error(context: Any, code: int, info: str) -> None:
    _log("GM_ERROR", code=code, info=info)
