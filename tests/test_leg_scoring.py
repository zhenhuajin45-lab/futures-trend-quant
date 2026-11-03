import json
from pathlib import Path

from backtest.leg_scoring import public_boundary_counts, score_reference_leg_events


def _event(
    confirmation: int,
    end: int,
    *,
    direction: str = "bear",
    complete: bool = True,
    status: str = "unresolved",
    resolved_index: int | None = None,
) -> dict[str, object]:
    return {
        "event_id": f"event-{confirmation}",
        "confirmation_index": confirmation,
        "observation_end_index": end,
        "observation_complete": complete,
        "new_direction": direction,
        "status": status,
        "resolved_index": resolved_index,
    }


def test_two_target_bars_inside_leg_are_resolved() -> None:
    events = [_event(1, 6), _event(5, 7, direction="bull")]
    score = score_reference_leg_events(
        ["bull", "bull", "bear", "bear", "bear", "bull", "bull", "bull"],
        events,
    )
    first = score["events"][0]
    assert first["leg_status"] == "resolved_in_leg"
    assert first["first_target_index"] == 2
    assert first["second_confirmation_index"] == 3
    assert first["leg_delay_bars"] == 1


def test_first_target_on_last_leg_bar_and_second_across_boundary_is_superseded() -> None:
    events = [_event(1, 6), _event(4, 7, direction="bull")]
    score = score_reference_leg_events(
        ["bull", "bull", "bull", "bear", "bear", "bull", "bull", "bull"],
        events,
    )
    first = score["events"][0]
    assert first["leg_valid_end_index"] == 3
    assert first["first_target_index"] == 3
    assert first["second_confirmation_index"] is None
    assert first["leg_status"] == "superseded"


def test_one_bar_leg_is_retained_as_too_short_and_tail_remains_censored() -> None:
    events = [
        _event(1, 6),
        _event(2, 6, direction="bull"),
        _event(5, 7, complete=False),
    ]
    score = score_reference_leg_events(
        ["bull", "bear", "bull", "bull", "bull", "bear", "bear", "bear"],
        events,
    )
    assert score["events"][0]["too_short"] is True
    assert score["events"][0]["leg_status"] == "superseded"
    assert score["events"][2]["leg_status"] == "censored"
    assert score["scoreable_events"] == 2
    assert score["censored_events"] == 1


def test_public_report_reproduces_frozen_boundary_counts() -> None:
    path = Path(__file__).parents[1] / "reports" / "sprint2_p0b_reference_comparison.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert public_boundary_counts(report) == {
        "scoreable": 793,
        "overlapping_windows": 613,
        "original_resolved": 630,
        "late_credit": 42,
        "resolved_in_leg": 588,
        "within_4_bars": 376,
    }
