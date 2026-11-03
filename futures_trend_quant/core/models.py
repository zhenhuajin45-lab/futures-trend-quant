from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import math
from typing import Any, Literal


Direction = Literal["bull", "bear"]
TrendStateName = Literal["none", "bull", "bear"]
SlopeState = Literal["up", "down", "flat", "unavailable"]


@dataclass(frozen=True)
class Bar:
    symbol: str
    frequency: str
    bob: datetime
    eob: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0
    position: float = 0.0
    pre_close: float | None = None

    def __post_init__(self) -> None:
        values = (self.open, self.high, self.low, self.close)
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("OHLC must be finite and positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another OHLC value")
        if self.eob <= self.bob:
            raise ValueError("eob must be after bob")

    @property
    def identity(self) -> str:
        return f"{self.symbol}|{self.frequency}|{self.eob.isoformat()}"


@dataclass(frozen=True)
class Pivot:
    index: int
    defined_at: int
    kind: Literal["high", "low"]
    price: float
    atr: float


@dataclass(frozen=True)
class MacdSegment:
    sign: Literal["positive", "negative"]
    start: int
    end: int
    peak_abs: float
    area_abs: float
    complete: bool


@dataclass(frozen=True)
class Divergence:
    kind: Literal["top", "bottom"]
    mode: Literal["A47", "B_hist"]
    previous_index: int
    current_index: int
    defined_at: int
    quality: int
    channels: tuple[str, ...]
    extreme_price: float


@dataclass
class TrendState:
    trend_state: TrendStateName = "none"
    since_bar_id: str | None = None
    trend_extreme: float | None = None
    blocked_dir: Direction | None = None
    leave_votes: int = 0
    establish_votes: int = 0
    establish_direction: Direction | None = None
    last_closed_bar_id: str | None = None
    facts_revision: str = "facts-v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TrendState":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unknown trend-state fields: {sorted(unknown)}")
        return cls(**raw)
