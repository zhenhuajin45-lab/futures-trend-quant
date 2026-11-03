from __future__ import annotations

from dataclasses import dataclass

from .models import Direction, PathName


@dataclass(frozen=True)
class CooldownDecision:
    allowed: bool
    reason: str


def evaluate_cooldown(
    *,
    path: PathName,
    direction: Direction,
    current_index: int,
    current_price: float,
    atr14: float,
    last_fill_index: int | None,
    last_fill_price: float | None,
    cooldown_bars: int = 4,
    path_d_exception_atr: float = 1.5,
) -> CooldownDecision:
    if cooldown_bars < 0 or path_d_exception_atr < 0:
        raise ValueError("cooldown parameters must be non-negative")
    if last_fill_index is None:
        return CooldownDecision(True, "NO_PRIOR_FILL")
    if current_index - last_fill_index >= cooldown_bars:
        return CooldownDecision(True, "ELAPSED")
    if path == "D" and last_fill_price is not None and atr14 > 0:
        favorable = (
            current_price - last_fill_price if direction == "bull" else last_fill_price - current_price
        )
        if favorable > path_d_exception_atr * atr14:
            return CooldownDecision(True, "PATH_D_DISTANCE_EXCEPTION")
    return CooldownDecision(False, "COOLDOWN_ACTIVE")
