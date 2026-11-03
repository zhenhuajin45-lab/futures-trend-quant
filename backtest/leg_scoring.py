from __future__ import annotations

from statistics import median
from typing import Mapping, Sequence


def _direction(value: object) -> int:
    if value in (1, "bull"):
        return 1
    if value in (-1, "bear"):
        return -1
    return 0


def score_reference_leg_events(
    predicted: Sequence[int | str | None],
    events: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Score two-bar confirmation without credit after the next reference turn.

    This is post-run evaluation code.  The strategy cannot import it and the
    next-event boundary is never exposed to replay state.
    """

    normalized = [_direction(value) for value in predicted]
    ordered = sorted(events, key=lambda row: int(row["confirmation_index"]))
    details: list[dict[str, object]] = []
    for index, raw in enumerate(ordered):
        event = dict(raw)
        confirmation = int(event["confirmation_index"])
        observation_end = int(event["observation_end_index"])
        if not 0 <= confirmation <= observation_end < len(normalized):
            raise ValueError("event observation window exceeds predicted sequence")
        next_confirmation = (
            int(ordered[index + 1]["confirmation_index"])
            if index + 1 < len(ordered)
            else None
        )
        valid_end = observation_end
        if next_confirmation is not None:
            valid_end = min(valid_end, next_confirmation - 1)
        event.update({
            "next_confirmation_index": next_confirmation,
            "leg_valid_end_index": valid_end,
            "too_short": valid_end - confirmation + 1 < 2,
            "original_status": event.get("status"),
            "original_resolved_index": event.get("resolved_index"),
        })
        if not bool(event.get("observation_complete", False)):
            event.update({
                "leg_status": "censored",
                "first_target_index": None,
                "second_confirmation_index": None,
                "leg_delay_bars": None,
                "within_4_bars": False,
                "target_at_confirmation": normalized[confirmation] == _direction(event["new_direction"]),
            })
            details.append(event)
            continue

        target = _direction(event["new_direction"])
        first_target = next(
            (item for item in range(confirmation, valid_end + 1) if normalized[item] == target),
            None,
        )
        resolved = next(
            (
                item
                for item in range(confirmation, valid_end)
                if normalized[item] == target and normalized[item + 1] == target
            ),
            None,
        )
        if resolved is not None:
            status = "resolved_in_leg"
            delay = resolved - confirmation
            second = resolved + 1
        else:
            superseded = next_confirmation is not None and next_confirmation <= observation_end
            status = "superseded" if superseded else "timeout"
            delay = None
            second = None
        event.update({
            "leg_status": status,
            "first_target_index": first_target,
            "second_confirmation_index": second,
            "leg_delay_bars": delay,
            "within_4_bars": delay is not None and delay <= 4,
            "target_at_confirmation": normalized[confirmation] == target,
        })
        details.append(event)

    return summarize_leg_event_details(details)


def summarize_leg_event_details(details: Sequence[Mapping[str, object]]) -> dict[str, object]:
    normalized_details = [dict(row) for row in details]
    scoreable = [row for row in normalized_details if row["leg_status"] != "censored"]
    resolved = [row for row in scoreable if row["leg_status"] == "resolved_in_leg"]
    successful_delays = [int(row["leg_delay_bars"]) for row in resolved]
    penalized = [
        int(row["leg_delay_bars"]) if row["leg_status"] == "resolved_in_leg" else 49
        for row in scoreable
    ]
    return {
        "reference_definition": "ATR_DIRECTIONAL_CHANGE_V1_LEG_VALIDITY",
        "events": normalized_details,
        "event_count": len(normalized_details),
        "scoreable_events": len(scoreable),
        "censored_events": sum(row["leg_status"] == "censored" for row in normalized_details),
        "resolved_in_leg": len(resolved),
        "within_4_bars": sum(bool(row["within_4_bars"]) for row in scoreable),
        "superseded": sum(row["leg_status"] == "superseded" for row in scoreable),
        "timeout": sum(row["leg_status"] == "timeout" for row in scoreable),
        "too_short": sum(bool(row["too_short"]) for row in scoreable),
        "target_at_confirmation": sum(bool(row["target_at_confirmation"]) for row in scoreable),
        "successful_delay_median": median(successful_delays) if successful_delays else None,
        "successful_delay_denominator": len(successful_delays),
        "penalized_delay_median": median(penalized) if penalized else None,
        "penalty_for_failure": 49,
    }


def public_boundary_counts(reference_report: Mapping[str, object]) -> dict[str, int]:
    """Reproduce the frozen public 793/613/630/42/588/376 audit."""

    scoreable = overlap = resolved = late_credit = timely = 0
    per_product = reference_report.get("per_product")
    if not isinstance(per_product, Mapping):
        raise ValueError("reference report has no per_product mapping")
    for product in per_product.values():
        if not isinstance(product, Mapping):
            raise ValueError("invalid product report")
        independent = product.get("independent_turn_events")
        if not isinstance(independent, Mapping) or not isinstance(independent.get("events"), list):
            raise ValueError("product report has no independent events")
        events = sorted(independent["events"], key=lambda row: int(row["confirmation_index"]))
        for index, event in enumerate(events):
            if event.get("status") == "censored":
                continue
            scoreable += 1
            next_turn = (
                int(events[index + 1]["confirmation_index"])
                if index + 1 < len(events)
                else 10**18
            )
            overlap += int(next_turn <= int(event["observation_end_index"]))
            if event.get("status") != "resolved":
                continue
            resolved += 1
            resolved_index = int(event["resolved_index"])
            both_in_leg = resolved_index + 1 < next_turn
            late_credit += int(not both_in_leg)
            timely += int(both_in_leg and int(event["delay_bars"]) <= 4)
    return {
        "scoreable": scoreable,
        "overlapping_windows": overlap,
        "original_resolved": resolved,
        "late_credit": late_credit,
        "resolved_in_leg": resolved - late_credit,
        "within_4_bars": timely,
    }
