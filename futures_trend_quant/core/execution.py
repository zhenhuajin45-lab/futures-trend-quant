from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Direction = Literal["bull", "bear"]
TradeStatus = Literal["pending_open", "open", "pending_close"]


@dataclass
class TradeRecord:
    symbol: str
    product: str
    cluster: str
    direction: Direction
    volume: int
    planned_entry: float
    entry_price: float
    risk_distance: float
    stop: float
    target: float
    multiplier: float
    price_tick: float
    evidence_id: str
    opened_bar_id: str
    csymbol: str = ""
    path: str = "D"
    signal_m15_index: int = 0
    status: TradeStatus = "pending_open"
    open_order_id: str = ""
    close_order_id: str = ""
    close_reason: str = ""
    pending_exit_reason: str = ""
    pm_mode: str = "fixed"
    break_even_armed: bool = False

    @property
    def open_risk(self) -> float:
        return abs(self.entry_price - self.stop) * self.volume * self.multiplier


def exit_reason(record: TradeRecord, *, high: float, low: float) -> str | None:
    """Conservative same-bar rule: stop wins when stop and target are both touched."""
    if record.direction == "bull":
        if low <= record.stop:
            return "BREAK_EVEN_STOP" if record.break_even_armed else "STOP"
        if high >= record.target:
            return "TARGET"
    else:
        if high >= record.stop:
            return "BREAK_EVEN_STOP" if record.break_even_armed else "STOP"
        if low <= record.target:
            return "TARGET"
    return None


def should_arm_break_even(
    *,
    direction: Direction,
    entry_price: float,
    risk_distance: float,
    high: float,
    low: float,
) -> bool:
    if entry_price <= 0 or risk_distance <= 0:
        raise ValueError("break-even management requires positive entry and risk distance")
    if direction == "bull":
        return high >= entry_price + risk_distance
    return low <= entry_price - risk_distance
