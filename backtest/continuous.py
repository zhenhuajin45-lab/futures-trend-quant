from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable, Mapping

from futures_trend_quant.core.models import Bar


@dataclass(frozen=True)
class ContractBar:
    trading_date: date
    contract: str
    bar: Bar


@dataclass(frozen=True)
class RollEvent:
    trading_date: date
    old_contract: str
    new_contract: str
    price_difference: float


def build_additive_adjustments(rolls: Iterable[RollEvent]) -> dict[date, float]:
    """Return self-back-adjustment offsets keyed by effective roll date.

    On a roll from old to new, ``price_difference = new - old``.  From the
    roll date onward the cumulative offset subtracts that difference, keeping
    the continuous series level with the preceding contract.
    """

    cumulative = 0.0
    output: dict[date, float] = {}
    for event in sorted(rolls, key=lambda item: item.trading_date):
        cumulative -= event.price_difference
        output[event.trading_date] = cumulative
    return output


def apply_additive_back_adjustment(
    rows: Iterable[ContractBar],
    main_mapping: Mapping[date, str],
    rolls: Iterable[RollEvent],
    *,
    continuous_symbol: str | None = None,
) -> list[Bar]:
    adjustments = build_additive_adjustments(rolls)
    roll_dates = sorted(adjustments)
    output: list[Bar] = []
    for row in sorted(rows, key=lambda item: item.bar.eob):
        expected = main_mapping.get(row.trading_date)
        if expected is None:
            raise ValueError(f"missing main mapping for {row.trading_date}")
        if row.contract != expected:
            continue
        offset = 0.0
        for roll_date in roll_dates:
            if roll_date <= row.trading_date:
                offset = adjustments[roll_date]
            else:
                break
        bar = row.bar
        adjusted_prices = {
            "open": bar.open + offset,
            "high": bar.high + offset,
            "low": bar.low + offset,
            "close": bar.close + offset,
            "pre_close": None if bar.pre_close is None else bar.pre_close + offset,
        }
        if min(adjusted_prices[key] for key in ("open", "high", "low", "close")) <= 0:
            raise ValueError("additive adjustment produced a non-positive price")
        output.append(
            replace(
                bar,
                symbol=continuous_symbol if continuous_symbol is not None else expected,
                **adjusted_prices,
            )
        )
    return output
