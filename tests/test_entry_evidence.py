from __future__ import annotations

from datetime import datetime, timedelta

from futures_trend_quant.core.entry import (
    EntryContext,
    EntrySettings,
    EvidenceEvent,
    EvidenceSnapshot,
    evaluate_entries,
)


def _context(**overrides) -> EntryContext:
    values = dict(
        direction="bear",
        trend_confirmed=True,
        h1_state="flat",
        m15_slope_state="down",
        m15_reverse_divergence=False,
        gate_open=True,
        m15_index=10,
        current_price=100.0,
        atr14=1.0,
    )
    values.update(overrides)
    return EntryContext(**values)


def test_evidence_id_is_unique_path_direction_trigger_tuple() -> None:
    evidence = EvidenceSnapshot(
        direction="bear",
        m5_divergence=EvidenceEvent("top", "bear", "m5-7", 7),
        m5_flatten=EvidenceEvent("flat", "bear", "m5-9", 9),
    )
    settings = EntrySettings(path_b_enabled=False, path_c_enabled=False, path_d_enabled=False)
    first = evaluate_entries(_context(), evidence, settings)
    assert first.evidence_id == "A|bear|m5-9"
    second = evaluate_entries(
        _context(used_evidence_ids=frozenset({first.evidence_id})), evidence, settings
    )
    assert not second.accepted
    assert second.reason == "EVIDENCE_ALREADY_USED"


def test_optional_m1_confirmation_must_be_after_selected_m5_trigger() -> None:
    now = datetime(2026, 11, 3, 9, 0)
    evidence = EvidenceSnapshot(
        direction="bear",
        m5_divergence=EvidenceEvent("top", "bear", "m5-7", 7, now),
        m5_flatten=EvidenceEvent("flat", "bear", "m5-9", 9, now + timedelta(minutes=5)),
        m1_confirmation=EvidenceEvent("m1", "bear", "m1-1", 1, now + timedelta(minutes=4)),
    )
    settings = EntrySettings(
        path_b_enabled=False, path_c_enabled=False, path_d_enabled=False, m1_confirmation_enabled=True
    )
    decision = evaluate_entries(_context(), evidence, settings)
    assert not decision.accepted
    assert decision.reason == "M1_CONFIRMATION_BEFORE_TRIGGER"
