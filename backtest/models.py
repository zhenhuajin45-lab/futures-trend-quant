from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from futures_trend_quant.core.models import Bar


Side = Literal["long", "short"]
Offset = Literal["open", "close", "close_today"]


@dataclass(frozen=True)
class FeeSchedule:
    product: str
    multiplier: float
    price_tick: float
    open_rate: float = 0.0
    close_rate: float = 0.0
    close_today_rate: float = 0.0
    open_per_lot: float = 0.0
    close_per_lot: float = 0.0
    close_today_per_lot: float = 0.0
    slippage_ticks: int = 1
    verified: bool = False

    def __post_init__(self) -> None:
        if self.multiplier <= 0 or self.price_tick <= 0:
            raise ValueError("multiplier and price_tick must be positive")
        values = (
            self.open_rate,
            self.close_rate,
            self.close_today_rate,
            self.open_per_lot,
            self.close_per_lot,
            self.close_today_per_lot,
        )
        if any(value < 0 for value in values):
            raise ValueError("fee values cannot be negative")
        if self.slippage_ticks < 0:
            raise ValueError("slippage_ticks cannot be negative")

    def fee(self, price: float, volume: int, offset: Offset) -> float:
        if volume <= 0:
            raise ValueError("volume must be positive")
        rate = {
            "open": self.open_rate,
            "close": self.close_rate,
            "close_today": self.close_today_rate,
        }[offset]
        per_lot = {
            "open": self.open_per_lot,
            "close": self.close_per_lot,
            "close_today": self.close_today_per_lot,
        }[offset]
        return price * self.multiplier * volume * rate + volume * per_lot


@dataclass(frozen=True)
class SignalIntent:
    symbol: str
    side: Side
    volume: int
    stop_price: float
    target_price: float
    path: str
    created_at: datetime
    entry_price: float | None = None
    allow_stack: bool = False
    evidence_id: str = ""
    signal_m15_index: int = 0

    def __post_init__(self) -> None:
        if self.volume <= 0:
            raise ValueError("volume must be positive")
        if min(self.stop_price, self.target_price) <= 0:
            raise ValueError("stop and target must be positive")
        if self.side == "long" and self.stop_price >= self.target_price:
            raise ValueError("long stop must be below target")
        if self.side == "short" and self.stop_price <= self.target_price:
            raise ValueError("short stop must be above target")


@dataclass
class Position:
    symbol: str
    side: Side
    volume: int
    entry_price: float
    stop_price: float
    target_price: float
    opened_at: datetime
    trading_date: date
    path: str
    evidence_id: str = ""
    signal_m15_index: int = 0
    entry_fee: float = 0.0


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: Side
    offset: Offset
    volume: int
    price: float
    timestamp: datetime
    fee: float
    reason: str


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: Side
    volume: int
    path: str
    opened_at: datetime
    closed_at: datetime
    entry_price: float
    exit_price: float
    gross_pnl: float
    fees: float
    net_pnl: float
    reason: str
    holding_bars: int = 0
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TimeframeClose:
    timeframe: str
    bar: Bar
