from datetime import datetime, timedelta

from backtest.turn_audit import extract_turn_events, summarize_turn_events


def _row(index: int, state: str, *, action: str = "maintain", reason: str = "MAINTAIN") -> dict[str, object]:
    eob = datetime(2026, 1, 5, 9) + timedelta(minutes=15 * index)
    return {
        "eob": eob.isoformat(),
        "h1_absolute": "bull_strong",
        "top_div": False,
        "bottom_div": False,
        "trend_inputs": {
            "s1_extreme_then_flat": False,
            "s2_bearish_pattern": False,
            "s2_bullish_pattern": False,
            "p_close_cross": False,
            "p_macd_cross": False,
            "r_bearish_reverse": False,
            "r_bullish_reverse": False,
            "h1_relative": "strong",
        },
        "trend_decision": {
            "proposed_action": action,
            "reason_code": reason,
            "state": {"trend_state": state},
        },
    }


def test_turn_audit_uses_two_consecutive_target_states() -> None:
    rows = [
        _row(0, "bull"),
        _row(1, "bull"),
        _row(2, "bear", action="veto", reason="VETO"),
        _row(3, "bull"),
        _row(4, "bear", action="flip", reason="LEAVE_VOTE"),
        _row(5, "bear", action="flip", reason="LEAVE_VOTE"),
    ]
    events = extract_turn_events(rows, [1, 1, -1, -1, -1, -1], csymbol="TEST.X")
    assert len(events) == 1
    assert events[0]["delay_bars"] == 2
    assert events[0]["resolved"] is True
    assert events[0]["window_action_counts"] == {"flip": 1, "maintain": 1, "veto": 1}


def test_turn_audit_summarizes_unresolved_and_veto_exposure() -> None:
    rows = [
        _row(0, "bull"),
        _row(1, "bull"),
        _row(2, "bull", action="veto", reason="VETO"),
        _row(3, "bull", action="maintain", reason="MAINTAIN"),
    ]
    events = extract_turn_events(rows, [1, 1, -1, -1], csymbol="TEST.X")
    summary = summarize_turn_events(events)
    assert summary["turn_events"] == 1
    assert summary["delay_median"] == 49
    assert summary["unresolved_rate"] == 1.0
    assert summary["delay_buckets"] == {"unresolved": 1}
    assert summary["delayed_at_turn_state_relation_counts"] == {"old_direction": 1}
    assert summary["delayed_at_turn_h1_relation_counts"] == {"old_direction_strong": 1}
    assert summary["delayed_at_turn_leave_inputs_available"] == 1
    assert summary["delayed_at_turn_leave_input_true_counts"]["path1"] == 0
    assert summary["delayed_at_turn_leave_input_true_counts"]["path2"] == 0
    assert summary["delayed_old_state_leave_inputs_available"] == 1
    assert summary["delayed_old_state_leave_input_true_counts"]["path1"] == 0
    assert summary["delayed_events_with_any_veto"] == 1
    assert summary["delayed_events_with_any_maintain"] == 1
