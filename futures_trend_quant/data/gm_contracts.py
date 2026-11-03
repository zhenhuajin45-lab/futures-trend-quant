from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from ..config import Product


class ContractResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class MainContract:
    csymbol: str
    symbol: str
    trade_date: str


def normalize_trade_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if len(text) >= 10:
        return text[:10]
    raise ContractResolutionError(f"invalid trade_date: {value!r}")


def resolve_main_contracts(
    products: Iterable[Product],
    trade_date: str,
    fetch: Callable[..., list[dict[str, Any]]],
) -> tuple[MainContract, ...]:
    resolved: list[MainContract] = []
    failures: list[str] = []
    for product in products:
        try:
            rows = fetch(csymbol=product.csymbol, start_date=trade_date, end_date=trade_date)
        except Exception as exc:  # adapter boundary; reported and fail-closed below
            failures.append(f"{product.csymbol}:api_error:{type(exc).__name__}")
            continue
        normalized_dates: list[str] = []
        for row in rows:
            try:
                normalized_dates.append(normalize_trade_date(row.get("trade_date")))
            except ContractResolutionError:
                normalized_dates.append("<invalid>")
        matching = [
            row
            for row, normalized_date in zip(rows, normalized_dates)
            if normalized_date == trade_date
        ]
        if len(matching) != 1 or not matching[0].get("symbol"):
            returned_dates = sorted(set(normalized_dates))[:3]
            returned_symbols = [str(row.get("symbol", "")) for row in rows[:3]]
            failures.append(
                f"{product.csymbol}:mapping_count={len(matching)}:row_count={len(rows)}"
                f":dates={returned_dates}:symbols={returned_symbols}"
            )
            continue
        resolved.append(MainContract(product.csymbol, str(matching[0]["symbol"]), trade_date))
    if failures:
        raise ContractResolutionError(";".join(failures))
    if not resolved:
        raise ContractResolutionError("no main contracts resolved")
    return tuple(resolved)


def diagnose_contract_history(
    products: Iterable[Product],
    trade_date: str,
    fetch: Callable[..., list[dict[str, Any]]],
    *,
    lookback_days: int = 14,
) -> dict[str, dict[str, Any]]:
    """Return bounded, non-sensitive evidence for continuous-contract failures."""
    end = datetime.fromisoformat(trade_date).date()
    start = (end - timedelta(days=lookback_days)).isoformat()
    diagnostics: dict[str, dict[str, Any]] = {}
    for product in products:
        try:
            rows = fetch(csymbol=product.csymbol, start_date=start, end_date=trade_date)
        except Exception as exc:
            diagnostics[product.csymbol] = {"error": type(exc).__name__}
            continue
        samples: list[dict[str, str]] = []
        for row in rows[-3:]:
            try:
                returned_date = normalize_trade_date(row.get("trade_date"))
            except ContractResolutionError:
                returned_date = "<invalid>"
            samples.append({"trade_date": returned_date, "symbol": str(row.get("symbol", ""))})
        diagnostics[product.csymbol] = {
            "start_date": start,
            "end_date": trade_date,
            "row_count": len(rows),
            "tail": samples,
        }
    return diagnostics


def contract_map(contracts: Iterable[MainContract]) -> dict[str, str]:
    return {contract.csymbol: contract.symbol for contract in contracts}
