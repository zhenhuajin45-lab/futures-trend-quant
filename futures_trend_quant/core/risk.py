from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal


Direction = Literal["bull", "bear"]


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    price_tick: float
    multiplier: float
    margin_ratio: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) and value > 0 for value in (self.price_tick, self.multiplier, self.margin_ratio)):
            raise ValueError("invalid contract specification")


@dataclass(frozen=True)
class StopPlan:
    direction: Direction
    entry: float
    stop: float
    target: float
    distance: float
    anchor: float


@dataclass(frozen=True)
class SizeResult:
    volume: int
    risk_budget: float
    risk_per_contract: float
    risk_volume: int
    margin_volume: int


def _round_down(value: float, tick: float) -> float:
    return math.floor((value + 1e-12) / tick) * tick


def _round_up(value: float, tick: float) -> float:
    return math.ceil((value - 1e-12) / tick) * tick


def build_stop_plan(
    *,
    direction: Direction,
    entry: float,
    anchor: float,
    atr: float,
    tick: float,
    buffer_atr: float = 0.6,
    min_atr: float = 1.5,
    max_atr: float = 6.0,
    reward_r: float = 2.0,
) -> StopPlan:
    if direction not in ("bull", "bear"):
        raise ValueError("unsupported direction")
    if not all(math.isfinite(value) and value > 0 for value in (entry, anchor, atr, tick)):
        raise ValueError("stop inputs must be finite and positive")
    if not (0 <= buffer_atr and 0 < min_atr <= max_atr and reward_r > 0):
        raise ValueError("invalid stop parameters")
    minimum = max(min_atr * atr, 2 * tick)
    if direction == "bull":
        structural = _round_down(anchor - buffer_atr * atr, tick)
        distance = max(entry - structural, minimum)
        if distance > max_atr * atr:
            raise ValueError("structural stop exceeds maximum ATR distance")
        stop = _round_down(entry - distance, tick)
        target = _round_up(entry + reward_r * (entry - stop), tick)
    else:
        structural = _round_up(anchor + buffer_atr * atr, tick)
        distance = max(structural - entry, minimum)
        if distance > max_atr * atr:
            raise ValueError("structural stop exceeds maximum ATR distance")
        stop = _round_up(entry + distance, tick)
        target = _round_down(entry - reward_r * (stop - entry), tick)
    final_distance = abs(entry - stop)
    if not (stop < entry < target if direction == "bull" else target < entry < stop):
        raise ValueError("invalid stop/target ordering")
    return StopPlan(direction, entry, stop, target, final_distance, anchor)


def calculate_size(
    *,
    equity: float,
    available: float,
    entry: float,
    stop_distance: float,
    spec: ContractSpec,
    risk_pct: float,
    risk_cap_abs: float | None,
    margin_usage_pct: float,
) -> SizeResult:
    if not all(math.isfinite(value) and value > 0 for value in (equity, available, entry, stop_distance)):
        raise ValueError("account and price inputs must be finite and positive")
    if not (0 < risk_pct <= 0.01):
        raise ValueError("risk_pct must be in (0, 0.01]")
    if risk_cap_abs is not None and (not math.isfinite(risk_cap_abs) or risk_cap_abs <= 0):
        raise ValueError("risk_cap_abs must be null or positive")
    if not (0 < margin_usage_pct <= 0.8):
        raise ValueError("margin_usage_pct must be in (0, 0.8]")
    risk_budget = equity * risk_pct
    if risk_cap_abs is not None:
        risk_budget = min(risk_budget, risk_cap_abs)
    risk_per_contract = stop_distance * spec.multiplier
    risk_volume = math.floor(risk_budget / risk_per_contract)
    margin_per_contract = entry * spec.multiplier * spec.margin_ratio
    margin_volume = math.floor(available * margin_usage_pct / margin_per_contract)
    return SizeResult(max(0, min(risk_volume, margin_volume)), risk_budget, risk_per_contract, risk_volume, margin_volume)
