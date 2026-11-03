from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Iterable


@dataclass(frozen=True)
class ProductSizingInput:
    product: str
    multiplier: float
    price_tick: float
    median_price: float
    atr14: float
    average_daily_volume: float
    estimated_round_trip_cost_per_lot: float

    def __post_init__(self) -> None:
        values = (
            self.multiplier,
            self.price_tick,
            self.median_price,
            self.atr14,
            self.average_daily_volume,
        )
        if any(value <= 0 for value in values):
            raise ValueError("sizing inputs must be positive and derived from actual history")
        if self.estimated_round_trip_cost_per_lot < 0:
            raise ValueError("cost cannot be negative")


def lots_for_stop(
    item: ProductSizingInput,
    *,
    capital: float,
    risk_pct: float,
    stop_atr: float,
    adv_fraction: float = 0.001,
) -> dict[str, float | int | bool]:
    if capital <= 0 or not 0 < risk_pct <= 0.01 or stop_atr <= 0:
        raise ValueError("invalid capital, risk_pct, or stop_atr")
    risk_per_lot = item.atr14 * stop_atr * item.multiplier + item.estimated_round_trip_cost_per_lot
    risk_lots = floor(capital * risk_pct / risk_per_lot)
    liquidity_cap = floor(item.average_daily_volume * adv_fraction)
    final_lots = max(0, min(risk_lots, liquidity_cap))
    return {
        "capital": capital,
        "risk_budget": capital * risk_pct,
        "stop_atr": stop_atr,
        "risk_per_lot": risk_per_lot,
        "risk_lots": risk_lots,
        "adv_cap_lots": liquidity_cap,
        "final_lots": final_lots,
        "tradable": final_lots >= 1,
    }


def build_sizing_matrix(
    inputs: Iterable[ProductSizingInput],
    *,
    capitals: tuple[float, ...] = (500_000.0, 1_000_000.0, 2_000_000.0),
    stop_atrs: tuple[float, ...] = (1.5, 3.0, 6.0),
    risk_pct: float = 0.01,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in inputs:
        for capital in capitals:
            for stop_atr in stop_atrs:
                rows.append(
                    {
                        "product": item.product,
                        "multiplier": item.multiplier,
                        "price_tick": item.price_tick,
                        "median_price": item.median_price,
                        "atr14": item.atr14,
                        "average_daily_volume": item.average_daily_volume,
                        **lots_for_stop(
                            item,
                            capital=capital,
                            risk_pct=risk_pct,
                            stop_atr=stop_atr,
                        ),
                    }
                )
    return rows
