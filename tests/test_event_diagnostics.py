from datetime import datetime, timedelta

from backtest.event_diagnostics import diagnose_event, summarize_diagnostics


def _row(
    index: int,
    *,
    state: str = "bull",
    top_div: bool = False,
    s1: bool = False,
    s2: bool = False,
    p: bool = False,
    r: bool = False,
    action: str = "maintain",
    reason: str = "MAINTAIN",
    confirmed: bool = False,
    confirmed_state: str = "bull",
) -> dict[str, object]:
    eob = datetime(2026, 1, 5, 9) + timedelta(minutes=15 * index)
    return {
        "symbol": "TEST.X1",
        "csymbol": "TEST.X",
        "eob": eob.isoformat(),
        "bar_id": f"TEST.X1|900s|{eob.isoformat()}",
        "top_div": top_div,
        "bottom_div": False,
        "h1_absolute": "bull_strong",
        "trend_inputs": {
            "s1_extreme_then_flat": s1,
            "s2_bearish_pattern": s2,
            "s2_bullish_pattern": False,
            "p_close_cross": p,
            "p_macd_cross": False,
            "r_bearish_reverse": r,
            "r_bullish_reverse": False,
            "h1_relative": "strong",
        },
        "trend_decision": {
            "proposed_action": action,
            "reason_code": reason,
            "confirmed": confirmed,
            "confirmed_state": confirmed_state,
            "state": {"trend_state": state, "leave_votes": int(reason == "LEAVE_VOTE")},
        },
    }


def test_event_diagnostic_records_first_components_votes_and_h1_shadows() -> None:
    rows = [
        _row(0),
        _row(1, top_div=True),
        _row(2, top_div=True, s2=True, p=True, action="veto", reason="VETO"),
        _row(3, top_div=True, s2=True, p=True, action="flip", reason="LEAVE_VOTE"),
        _row(
            4,
            state="bear",
            action="flip",
            reason="LEAVE_VOTE",
            confirmed=True,
            confirmed_state="bear",
        ),
        _row(5, state="bear", confirmed_state="bear"),
        _row(6, state="bear", confirmed_state="bear"),
    ]
    event = {
        "event_id": "dc-test",
        "source": "ATR_DIRECTIONAL_CHANGE_V1",
        "csymbol": "TEST.X",
        "old_direction": "bull",
        "new_direction": "bear",
        "pivot_eob": rows[1]["eob"],
        "confirmation_eob": rows[2]["eob"],
        "confirmation_index": 2,
        "observation_end_index": 6,
        "observation_complete": True,
        "status": "resolved",
        "delay_bars": 2,
        "resolved_index": 4,
    }
    diagnostic = diagnose_event(
        rows,
        event,
        actual_contract="TEST.X1",
        trading_date="2026-01-05",
        roll_bucket="non_roll",
    )
    assert diagnostic["first_component_occurrences"]["divergence"]["delay_bars"] == -1
    assert diagnostic["first_component_occurrences"]["path1"]["delay_bars"] == 0
    assert diagnostic["first_leave_vote"]["delay_bars"] == 1
    assert diagnostic["first_new_direction_confirmation"]["delay_bars"] == 2
    assert diagnostic["h1_shadow"]["veto_blocked_end_evidence_rows"] == 1
    assert diagnostic["h1_shadow"]["veto_blocked_reverse_evidence_rows"] == 0
    assert diagnostic["strategy_state_at_window_end"] == "bear"

    summary = summarize_diagnostics([diagnostic])
    assert summary["events"] == 1
    assert summary["component_true_at_event"]["path1"] == 1
    assert summary["h1_shadow_events"]["veto_blocked_end_evidence"] == 1
