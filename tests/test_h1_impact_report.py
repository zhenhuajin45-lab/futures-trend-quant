from backtest.h1_impact_report import candidate_diffs


def test_selected_switch_and_formed_path_loss_have_separate_denominators():
    new = {"prior_state": "bear", "trend_after": "bull", "entry_direction": "bull",
           "h1_absolute": "bull_strong", "h1_source_eob": "2026-05-07T10:00:00", "accepted": True, "path": "D", "evidence_id": "d",
           "formed_paths": ["B", "D"], "path_b_h1_reason": "pass", "path_d_h1_reason": "pass",
           "reason": "PATH_D_ACCEPTED", "legacy_h1": {"accepted": True, "path": "B",
           "reason": "PATH_B_ACCEPTED", "evidence_id": "b", "formed_paths": ["B"]}}
    row = {"entry_evaluation": new, "h1_absolute": "flat", "eob": "2026-05-07T10:15:00"}
    counters, transitions, _ = candidate_diffs([row, {"entry_evaluation": None}])
    assert counters["complete_rows"] == 2 and counters["authorized_rows"] == 1
    assert counters["B_reselected_to_other"] == 1 and counters["B_formed_lost"] == 0
    assert counters["D_reselected_from_other"] == 1 and counters["D_formed_gained"] == 1
    assert counters["B_selected_changed_on_flip"] == counters["D_selected_changed_on_flip"] == 1
    assert transitions == {"B->D": 1}
