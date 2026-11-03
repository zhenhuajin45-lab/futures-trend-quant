from __future__ import annotations

from typing import Literal

from ..models import Bar, Pivot


def _is_high_pivot(bars: list[Bar], index: int, k: int) -> bool:
    price = bars[index].high
    return (
        all(price > bars[candidate].high for candidate in range(index - k, index))
        and all(price >= bars[candidate].high for candidate in range(index + 1, index + k + 1))
    )


def _is_low_pivot(bars: list[Bar], index: int, k: int) -> bool:
    price = bars[index].low
    return (
        all(price < bars[candidate].low for candidate in range(index - k, index))
        and all(price <= bars[candidate].low for candidate in range(index + 1, index + k + 1))
    )


def _more_extreme(candidate: Pivot, existing: Pivot) -> bool:
    if candidate.kind == "high":
        return candidate.price > existing.price
    return candidate.price < existing.price


def find_pivots(
    bars: list[Bar],
    atr_values: tuple[float | None, ...],
    *,
    k: int = 2,
    leg_min_atr: float = 1.2,
    mode: Literal["S_A", "S_B"] = "S_A",
) -> tuple[Pivot, ...]:
    if len(bars) != len(atr_values):
        raise ValueError("bars and atr_values length mismatch")
    if k < 1:
        raise ValueError("k must be positive")

    raw: list[Pivot] = []
    for index in range(k, len(bars) - k):
        atr_value = atr_values[index]
        if atr_value is None or atr_value <= 0:
            continue
        high = _is_high_pivot(bars, index, k)
        low = _is_low_pivot(bars, index, k)
        if high == low:
            continue
        raw.append(
            Pivot(
                index=index,
                defined_at=index + k,
                kind="high" if high else "low",
                price=bars[index].high if high else bars[index].low,
                atr=atr_value,
            )
        )

    alternating: list[Pivot] = []
    for pivot in raw:
        if not alternating:
            alternating.append(pivot)
            continue
        last = alternating[-1]
        if pivot.kind == last.kind:
            if _more_extreme(pivot, last):
                alternating[-1] = pivot
            continue
        if mode == "S_A" and abs(pivot.price - last.price) < leg_min_atr * last.atr:
            continue
        alternating.append(pivot)
    return tuple(alternating)


def previous_swing_pair(
    pivots: tuple[Pivot, ...],
    bars: list[Bar],
    *,
    current_index: int,
    kind: Literal["high", "low"],
    min_span_bars: int = 5,
    max_span_bars: int = 120,
    new_extreme_min_atr: float = 0.1,
) -> tuple[Pivot, Pivot] | None:
    current = next((pivot for pivot in reversed(pivots) if pivot.index == current_index and pivot.kind == kind), None)
    if current is None:
        return None
    candidates = [pivot for pivot in pivots if pivot.kind == kind and pivot.index < current.index]
    for previous in reversed(candidates):
        span = current.index - previous.index
        if not min_span_bars <= span <= max_span_bars:
            continue
        between = [pivot for pivot in pivots if previous.index < pivot.index < current.index]
        opposite = "low" if kind == "high" else "high"
        if not any(pivot.kind == opposite for pivot in between):
            continue
        threshold = new_extreme_min_atr * current.atr
        if kind == "high" and current.price < previous.price + threshold:
            continue
        if kind == "low" and current.price > previous.price - threshold:
            continue
        if kind == "high" and any(bar.high > max(previous.price, current.price) for bar in bars[previous.index + 1 : current.index]):
            continue
        if kind == "low" and any(bar.low < min(previous.price, current.price) for bar in bars[previous.index + 1 : current.index]):
            continue
        return previous, current
    return None
