from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..models import Bar, Pivot
from .models import Direction


AnchorMode = Literal["swing", "range20"]


@dataclass(frozen=True)
class AnchorSelection:
    direction: Direction
    mode: AnchorMode
    anchor_index: int
    anchor_price: float
    a_line_index: int | None = None
    a_line_price: float | None = None
    a_line_broken: bool = False
    anchor_bar_id: str | None = None
    a_line_bar_id: str | None = None


def _next_pivot(pivots: tuple[Pivot, ...], *, after: int, kind: str) -> Pivot | None:
    return next((pivot for pivot in pivots if pivot.index > after and pivot.kind == kind), None)


def _swing_anchor(
    bars: list[Bar],
    pivots: tuple[Pivot, ...],
    direction: Direction,
    previous: AnchorSelection | None,
) -> AnchorSelection | None:
    current_index = len(bars) - 1
    confirmed = tuple(pivot for pivot in pivots if pivot.defined_at <= current_index)
    anchor_kind = "low" if direction == "bull" else "high"
    a_kind = "high" if direction == "bull" else "low"

    if previous is not None and previous.direction == direction and previous.mode == "swing":
        if previous.anchor_bar_id is not None and all(
            bars[pivot.index].identity != previous.anchor_bar_id for pivot in confirmed
        ):
            # The structural anchor predates the current calculation window.
            # Preserve it rather than remapping its old relative index onto a
            # different bar. A longer persisted bar history is required before
            # the A-line can be advanced safely.
            return previous
        anchor = next(
            (
                pivot
                for pivot in confirmed
                if (
                    previous.anchor_bar_id is not None
                    and bars[pivot.index].identity == previous.anchor_bar_id
                )
                or (previous.anchor_bar_id is None and pivot.index == previous.anchor_index)
            ),
            None,
        )
        if anchor is None:
            anchor = Pivot(previous.anchor_index, previous.anchor_index, anchor_kind, previous.anchor_price, 0.0)
        a_line = (
            next(
                (
                    pivot
                    for pivot in confirmed
                    if (
                        previous.a_line_bar_id is not None
                        and bars[pivot.index].identity == previous.a_line_bar_id
                    )
                    or (previous.a_line_bar_id is None and pivot.index == previous.a_line_index)
                ),
                None,
            )
            if previous.a_line_index is not None or previous.a_line_bar_id is not None
            else _next_pivot(confirmed, after=anchor.index, kind=a_kind)
        )
        if a_line is None:
            return AnchorSelection(
                direction, "swing", anchor.index, anchor.price,
                anchor_bar_id=bars[anchor.index].identity,
            )
        broken_now = bars[-1].close > a_line.price if direction == "bull" else bars[-1].close < a_line.price
        broken = previous.a_line_broken or broken_now
        if not broken:
            # Pullbacks cannot move the anchor before the A-line breaks.
            return AnchorSelection(
                direction, "swing", anchor.index, anchor.price, a_line.index, a_line.price,
                anchor_bar_id=bars[anchor.index].identity,
                a_line_bar_id=bars[a_line.index].identity,
            )
        new_anchor = next(
            (pivot for pivot in reversed(confirmed) if pivot.kind == anchor_kind and pivot.index > a_line.index),
            None,
        )
        if new_anchor is None:
            return AnchorSelection(
                direction, "swing", anchor.index, anchor.price, a_line.index, a_line.price, True,
                bars[anchor.index].identity, bars[a_line.index].identity,
            )
        new_a = _next_pivot(confirmed, after=new_anchor.index, kind=a_kind)
        return AnchorSelection(
            direction, "swing", new_anchor.index, new_anchor.price,
            new_a.index if new_a else None, new_a.price if new_a else None,
            False, bars[new_anchor.index].identity,
            bars[new_a.index].identity if new_a else None,
        )

    # Bootstrap from the latest completed N leg. A lone pullback pivot is not
    # enough to replace the structural anchor.
    pairs = [
        (pivot, _next_pivot(confirmed, after=pivot.index, kind=a_kind))
        for pivot in confirmed
        if pivot.kind == anchor_kind
    ]
    completed = [(anchor, a_line) for anchor, a_line in pairs if a_line is not None]
    if not completed:
        return None
    anchor, a_line = completed[-1]
    return AnchorSelection(
        direction, "swing", anchor.index, anchor.price, a_line.index, a_line.price,
        False, bars[anchor.index].identity, bars[a_line.index].identity,
    )


def select_anchor(
    bars: list[Bar],
    direction: Direction,
    *,
    mode: AnchorMode = "swing",
    pivots: tuple[Pivot, ...] = (),
    previous: AnchorSelection | None = None,
    range_lookback: int = 20,
) -> AnchorSelection | None:
    if not bars:
        raise ValueError("bars must not be empty")
    if range_lookback < 1:
        raise ValueError("range_lookback must be positive")
    if mode == "swing":
        return _swing_anchor(bars, pivots, direction, previous)
    if mode != "range20":
        raise ValueError(f"unsupported anchor mode: {mode}")
    start = max(0, len(bars) - range_lookback)
    indices = range(start, len(bars))
    index = (
        min(indices, key=lambda item: bars[item].low)
        if direction == "bull"
        else max(indices, key=lambda item: bars[item].high)
    )
    price = bars[index].low if direction == "bull" else bars[index].high
    return AnchorSelection(
        direction, "range20", index, price,
        anchor_bar_id=bars[index].identity,
    )
