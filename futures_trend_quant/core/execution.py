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
    status: TradeStatus = "pending_open"
    open_order_id: str = ""
    close_order_id: str = ""
    close_reason: str = ""

    @property
    def open_risk(self) -> float:
        return abs(self.entry_price - self.stop) * self.volume * self.multiplier


def exit_reason(record: TradeRecord, *, high: float, low: float) -> str | None:
    """Conservative same-bar rule: stop wins when stop and target are both touched."""
    if record.direction == "bull":
        if low <= record.stop:
            return "STOP"
        if high >= record.target:
            return "TARGET"
    else:
        if high >= record.stop:
            return "STOP"
        if low <= record.target:
            return "TARGET"
    return None
