from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any

# Keep the platform boundary explicit. This module intentionally does not import
# order_volume, order_target_volume, order_value, order_percent, or cancel_order.
from gm.api import (  # type: ignore
    fut_get_continuous_contracts,
    get_next_n_trading_dates,
    schedule,
    subscribe,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.facts.indicators import calculate_indicators
from futures_trend_quant.data.gm_bars import bars_from_records
from futures_trend_quant.data.gm_contracts import (
    ContractResolutionError,
    diagnose_contract_history,
    resolve_main_contracts,
)


BAR_FIELDS = "symbol,frequency,open,high,low,close,volume,amount,position,bob,eob,pre_close"


def _log(tag: str, **payload: Any) -> None:
    print(f"[{tag}] {json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)}")


def _effective_trade_date(now: datetime) -> str:
    today = now.strftime("%Y-%m-%d")
    if now.hour <= 15:
        return today
    dates = get_next_n_trading_dates(exchange="SHSE", date=today, n=1)
    if not dates:
        raise ContractResolutionError(f"no next trading date after {today}")
    return str(dates[0])[:10]


def _refresh_contracts(context: Any) -> None:
    config = context.ftq_config
    trade_date = _effective_trade_date(context.now)
    try:
        contracts = resolve_main_contracts(config.products, trade_date, fut_get_continuous_contracts)
    except ContractResolutionError:
        _log(
            "CONTRACT_DIAG",
            trade_date=trade_date,
            history=diagnose_contract_history(
                config.products,
                trade_date,
                fut_get_continuous_contracts,
            ),
        )
        raise
    new_map = {contract.csymbol: contract.symbol for contract in contracts}
    symbols = list(new_map.values())
    if len(set(symbols)) != len(config.products):
        raise ContractResolutionError("resolved main contracts are not unique")

    changed = new_map != getattr(context, "ftq_contracts", {})
    context.ftq_contracts = new_map
    context.ftq_symbols = frozenset(symbols)
    for index, frequency in enumerate(config.frequencies):
        subscribe(
            symbols=symbols,
            frequency=frequency,
            count=config.subscription_count,
            wait_group=False,
            unsubscribe_previous=(index == 0),
            fields=BAR_FIELDS,
            format="df",
        )
    _log("CONTRACT_REFRESH", trade_date=trade_date, changed=changed, mappings=new_map)


def init(context: Any) -> None:
    config = load_config(default_config_path())
    if config.execute_enabled:
        raise RuntimeError("fail-closed: this build does not support order execution")
    context.ftq_config = config
    context.ftq_contracts = {}
    context.ftq_symbols = frozenset()
    _refresh_contracts(context)
    schedule(schedule_func=_refresh_contracts, date_rule="1d", time_rule="08:55:00")
    schedule(schedule_func=_refresh_contracts, date_rule="1d", time_rule="20:55:00")
    _log(
        "INIT",
        strategy=config.raw["strategy_name"],
        execute_enabled=False,
        products=[product.csymbol for product in config.products],
        frequencies=list(config.frequencies),
    )


def _records_from_dataframe(frame: Any) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    return frame.to_dict("records")


def on_bar(context: Any, bars: list[Any]) -> None:
    if not bars:
        _log("BAR_REJECT", reason="empty_callback")
        return
    for raw in bars:
        try:
            # GM's BarLikeDict2 follows the documented subscript API but is not
            # guaranteed to satisfy Python's dict(mapping) protocol.
            symbol = str(raw["symbol"])
            frequency = str(raw["frequency"])
        except (KeyError, TypeError, AttributeError):
            _log("BAR_REJECT", reason="missing_identity_fields")
            continue
        if symbol not in context.ftq_symbols or frequency not in context.ftq_config.frequencies:
            _log("BAR_REJECT", reason="out_of_scope", symbol=symbol, frequency=frequency)
            continue
        try:
            frame = context.data(
                symbol=symbol,
                frequency=frequency,
                count=context.ftq_config.subscription_count,
                fields=BAR_FIELDS,
            )
            records = _records_from_dataframe(frame)
            series = bars_from_records(records)
            if not series:
                raise ValueError("empty data window")
        except Exception as exc:
            _log("BAR_REJECT", reason="window_invalid", symbol=symbol, frequency=frequency, error=type(exc).__name__)
            continue

        latest = series[-1]
        _log(
            "BAR_CLOSED",
            bar_id=latest.identity,
            symbol=symbol,
            frequency=frequency,
            eob=latest.eob,
            close=latest.close,
            pre_close=latest.pre_close,
            window=len(series),
        )
        if frequency != "900s":
            continue
        if len(series) < int(context.ftq_config.raw["data"]["indicator_warmup_bars"]):
            _log("M15_WARMUP", symbol=symbol, have=len(series), need=context.ftq_config.raw["data"]["indicator_warmup_bars"])
            continue
        try:
            indicators = calculate_indicators(series)
            index = len(series) - 1
            _log(
                "M15_FACTS",
                symbol=symbol,
                bar_id=latest.identity,
                ema34=indicators.ema34[index],
                atr14=indicators.atr14[index],
                dif=indicators.dif[index],
                dea=indicators.dea[index],
                hist=indicators.hist[index],
                slope_ratio=indicators.slope_ratio[index],
                slope_state=indicators.slope_state[index],
                decision="observe_only",
            )
        except Exception as exc:
            _log("M15_FACTS_REJECT", symbol=symbol, bar_id=latest.identity, error=type(exc).__name__)


def on_error(context: Any, code: int, info: str) -> None:
    _log("GM_ERROR", code=code, info=info)
