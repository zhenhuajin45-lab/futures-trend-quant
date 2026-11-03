from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from statistics import median
from typing import Literal, Sequence

from futures_trend_quant.core.models import Bar


Direction = Literal["bull", "bear"]


@dataclass(frozen=True)
class ConfirmedTurnEvent:
    event_id: str
    source: str
    symbol: str
    old_direction: Direction
    new_direction: Direction
    pivot_index: int
    confirmation_index: int
    pivot_eob: str
    confirmation_eob: str
    pivot_price: float
    pivot_atr: float
    threshold_price: float
    observation_end_index: int
    observation_complete: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DirectionalChangeReference:
    labels: tuple[int | None, ...]
    events: tuple[ConfirmedTurnEvent, ...]
    initial_direction: Direction | None
    initial_confirmation_index: int | None


def _event_id(symbol: str, old: Direction, new: Direction, pivot_eob: str, confirmation_eob: str) -> str:
    raw = f"ATR_DIRECTIONAL_CHANGE_V1|{symbol}|{old}|{new}|{pivot_eob}|{confirmation_eob}"
    return "dc-" + sha256(raw.encode("utf-8")).hexdigest()[:20]


def atr_directional_change_reference(
    bars: Sequence[Bar],
    atr: Sequence[float | None],
    *,
    threshold_atr: float = 3.0,
    observation_bars: int = 48,
) -> DirectionalChangeReference:
    """Build confirmed close-based ATR directional-change events for one symbol.

    The function is post-run evaluation code.  It is deliberately not imported
    by the strategy core or persisted runtime.
    """

    if len(bars) != len(atr):
        raise ValueError("bars and atr must have equal length")
    if threshold_atr <= 0 or observation_bars < 2:
        raise ValueError("threshold_atr must be positive and observation_bars at least 2")
    if not bars:
        return DirectionalChangeReference((), (), None, None)
    symbols = {bar.symbol for bar in bars}
    if len(symbols) != 1:
        raise ValueError("directional-change reference must not cross symbols")
    if any(bars[index].eob <= bars[index - 1].eob for index in range(1, len(bars))):
        raise ValueError("bars must be strictly increasing")

    labels: list[int | None] = [None] * len(bars)
    first_valid = next(
        (index for index, value in enumerate(atr) if value is not None and value > 0),
        None,
    )
    if first_valid is None:
        return DirectionalChangeReference(tuple(labels), (), None, None)

    anchor_price = bars[first_valid].close
    anchor_atr = float(atr[first_valid])  # type: ignore[arg-type]
    direction: Direction | None = None
    initial_direction: Direction | None = None
    initial_confirmation: int | None = None
    extreme_index = first_valid
    extreme_price = anchor_price
    extreme_atr = anchor_atr
    events: list[ConfirmedTurnEvent] = []

    for index in range(first_valid + 1, len(bars)):
        value = atr[index]
        valid_atr = value is not None and value > 0
        close = bars[index].close
        if direction is None:
            if close >= anchor_price + threshold_atr * anchor_atr:
                direction = "bull"
            elif close <= anchor_price - threshold_atr * anchor_atr:
                direction = "bear"
            else:
                continue
            initial_confirmation = index
            initial_direction = direction
            labels[index] = 1 if direction == "bull" else -1
            extreme_index = index
            extreme_price = close
            extreme_atr = float(value) if valid_atr else anchor_atr
            continue

        if valid_atr:
            if direction == "bull" and close > extreme_price:
                extreme_index = index
                extreme_price = close
                extreme_atr = float(value)
            elif direction == "bear" and close < extreme_price:
                extreme_index = index
                extreme_price = close
                extreme_atr = float(value)

        old_direction = direction
        threshold_price = (
            extreme_price - threshold_atr * extreme_atr
            if old_direction == "bull"
            else extreme_price + threshold_atr * extreme_atr
        )
        reversal = (
            close <= threshold_price if old_direction == "bull" else close >= threshold_price
        )
        if reversal and valid_atr:
            new_direction: Direction = "bear" if old_direction == "bull" else "bull"
            end_index = index + observation_bars
            event = ConfirmedTurnEvent(
                event_id=_event_id(
                    bars[index].symbol,
                    old_direction,
                    new_direction,
                    bars[extreme_index].eob.isoformat(),
                    bars[index].eob.isoformat(),
                ),
                source="ATR_DIRECTIONAL_CHANGE_V1",
                symbol=bars[index].symbol,
                old_direction=old_direction,
                new_direction=new_direction,
                pivot_index=extreme_index,
                confirmation_index=index,
                pivot_eob=bars[extreme_index].eob.isoformat(),
                confirmation_eob=bars[index].eob.isoformat(),
                pivot_price=extreme_price,
                pivot_atr=extreme_atr,
                threshold_price=threshold_price,
                observation_end_index=min(end_index, len(bars) - 1),
                observation_complete=end_index < len(bars),
            )
            events.append(event)
            direction = new_direction
            extreme_index = index
            extreme_price = close
            extreme_atr = float(value)
        labels[index] = 1 if direction == "bull" else -1

    return DirectionalChangeReference(
        tuple(labels), tuple(events), initial_direction, initial_confirmation
    )


def score_confirmed_turn_events(
    predicted: Sequence[int | str | None],
    events: Sequence[ConfirmedTurnEvent],
) -> dict[str, object]:
    """Score only independent confirmed events; tail-censored events are excluded."""

    normalized = [
        1 if value in (1, "bull") else -1 if value in (-1, "bear") else 0
        for value in predicted
    ]
    if any(event.observation_end_index >= len(normalized) for event in events):
        raise ValueError("event observation window exceeds predicted sequence")
    details: list[dict[str, object]] = []
    for event in events:
        target = 1 if event.new_direction == "bull" else -1
        detail = event.to_dict()
        if not event.observation_complete:
            detail.update({"status": "censored", "delay_bars": None, "resolved_index": None})
            details.append(detail)
            continue
        resolved_index: int | None = None
        last_first = event.observation_end_index - 1
        for candidate in range(event.confirmation_index, last_first + 1):
            if normalized[candidate] == target and normalized[candidate + 1] == target:
                resolved_index = candidate
                break
        if resolved_index is None:
            detail.update({"status": "unresolved", "delay_bars": 49, "resolved_index": None})
        else:
            delay = resolved_index - event.confirmation_index
            detail.update({"status": "resolved", "delay_bars": delay, "resolved_index": resolved_index})
        details.append(detail)

    summary = summarize_scored_event_details(details)
    summary["events"] = details
    return summary


def summarize_scored_event_details(details: Sequence[dict[str, object]]) -> dict[str, object]:
    """Rebuild the event summary from auditable event-detail rows."""

    delays = [
        int(detail["delay_bars"])
        for detail in details
        if detail.get("status") in {"resolved", "unresolved"}
    ]
    unresolved = sum(detail.get("status") == "unresolved" for detail in details)
    censored = sum(detail.get("status") == "censored" for detail in details)
    ordered = sorted(delays)

    def percentile(p: float) -> float:
        if not ordered:
            return 0.0
        position = (len(ordered) - 1) * p
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    scoreable = len(delays)
    return {
        "reference_definition": "ATR_DIRECTIONAL_CHANGE_V1",
        "turn_events": len(details),
        "scoreable_turn_events": scoreable,
        "censored_turn_events": censored,
        "resolved_turn_events": scoreable - unresolved,
        "unresolved_turn_events": unresolved,
        "unresolved_turn_rate": unresolved / scoreable if scoreable else 0.0,
        "turn_delay_median": median(delays) if delays else 0.0,
        "turn_delay_p75": percentile(0.75),
        "turn_delay_p90": percentile(0.90),
        "turn_delay_passed": bool(delays) and median(delays) <= 4,
    }
