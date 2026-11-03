from __future__ import annotations

from dataclasses import dataclass

from ..models import Bar


@dataclass(frozen=True)
class BarArithmetic:
    body_signed: float
    body_size: float
    upper_shadow: float
    lower_shadow: float
    body_contains_previous: bool
    close_minus_ema34: float | None
    close_ema34_cross: str | None

    @property
    def bullish(self) -> bool:
        return self.body_signed > 0

    @property
    def bearish(self) -> bool:
        return self.body_signed < 0

    @property
    def upper_pinbar(self) -> bool:
        return self.upper_shadow >= 2 * self.body_size and self.upper_shadow > self.lower_shadow

    @property
    def lower_pinbar(self) -> bool:
        return self.lower_shadow >= 2 * self.body_size and self.lower_shadow > self.upper_shadow


def calculate_bar_arithmetic(
    bar: Bar,
    previous: Bar | None,
    ema34: float | None,
    previous_ema34: float | None = None,
) -> BarArithmetic:
    body_signed = bar.close - bar.open
    body_size = abs(body_signed)
    upper_shadow = bar.high - max(bar.open, bar.close)
    lower_shadow = min(bar.open, bar.close) - bar.low

    contains = False
    if previous is not None:
        current_top = max(bar.open, bar.close)
        current_bottom = min(bar.open, bar.close)
        previous_top = max(previous.open, previous.close)
        previous_bottom = min(previous.open, previous.close)
        contains = current_top >= previous_top and current_bottom <= previous_bottom

    close_minus = None if ema34 is None else bar.close - ema34
    cross = None
    if previous is not None and ema34 is not None and previous_ema34 is not None:
        previous_delta = previous.close - previous_ema34
        current_delta = bar.close - ema34
        if previous_delta <= 0 < current_delta:
            cross = "up"
        elif previous_delta >= 0 > current_delta:
            cross = "down"
        else:
            cross = "none"

    return BarArithmetic(
        body_signed=body_signed,
        body_size=body_size,
        upper_shadow=upper_shadow,
        lower_shadow=lower_shadow,
        body_contains_previous=contains,
        close_minus_ema34=close_minus,
        close_ema34_cross=cross,
    )


def is_bearish_engulfing(current: BarArithmetic, previous: BarArithmetic) -> bool:
    return current.bearish and previous.bullish and current.body_contains_previous


def is_bullish_engulfing(current: BarArithmetic, previous: BarArithmetic) -> bool:
    return current.bullish and previous.bearish and current.body_contains_previous
