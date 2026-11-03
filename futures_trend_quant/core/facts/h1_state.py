from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


H1Absolute = Literal["bull_strong", "bull_weakening", "flat", "bear_strong", "bear_weakening"]


@dataclass(frozen=True)
class H1StateResult:
    absolute: H1Absolute
    delta_atr: float
    monotonic_ratio: float
    close_side: Literal["above", "below", "equal"]
    divergence: Literal["none", "pending", "confirmed"]


def classify_h1_state(
    ema_values: list[float],
    *,
    latest_close: float,
    latest_atr: float,
    divergence: Literal["none", "pending", "confirmed"] = "none",
    n: int = 12,
    strong_atr: float = 0.6,
    mono_ratio: float = 0.75,
) -> H1StateResult:
    if len(ema_values) < n + 1:
        raise ValueError("insufficient H1 EMA history")
    if latest_atr <= 0:
        raise ValueError("latest_atr must be positive")
    recent = ema_values[-(n + 1) :]
    delta = (recent[-1] - recent[0]) / latest_atr
    changes = [recent[index] - recent[index - 1] for index in range(1, len(recent))]
    up_ratio = sum(change > 0 for change in changes) / n
    down_ratio = sum(change < 0 for change in changes) / n
    side: Literal["above", "below", "equal"]
    if latest_close > recent[-1]:
        side = "above"
    elif latest_close < recent[-1]:
        side = "below"
    else:
        side = "equal"

    if delta >= strong_atr and up_ratio >= mono_ratio:
        absolute: H1Absolute = "bull_weakening" if side == "below" or divergence == "confirmed" else "bull_strong"
        monotonic = up_ratio
    elif delta <= -strong_atr and down_ratio >= mono_ratio:
        absolute = "bear_weakening" if side == "above" or divergence == "confirmed" else "bear_strong"
        monotonic = down_ratio
    else:
        absolute = "flat"
        monotonic = max(up_ratio, down_ratio)
    return H1StateResult(
        absolute=absolute,
        delta_atr=delta,
        monotonic_ratio=monotonic,
        close_side=side,
        divergence=divergence,
    )


def relative_h1_state(absolute: H1Absolute, trend: Literal["bull", "bear"]) -> Literal["strong", "weakening", "flat", "reverse"]:
    if absolute == "flat":
        return "flat"
    if trend == "bull":
        if absolute == "bull_strong":
            return "strong"
        if absolute == "bull_weakening":
            return "weakening"
        return "reverse"
    if absolute == "bear_strong":
        return "strong"
    if absolute == "bear_weakening":
        return "weakening"
    return "reverse"
