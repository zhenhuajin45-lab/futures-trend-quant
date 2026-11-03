from __future__ import annotations

from futures_trend_quant.core.entry import (
    EntryContext,
    EntrySettings,
    EvidenceEvent,
    EvidenceSnapshot,
    evaluate_entries,
)


def event(kind: str, sequence: int, direction: str = "bull") -> EvidenceEvent:
    return EvidenceEvent(kind, direction, f"bar-{sequence}", sequence)


def context(**overrides) -> EntryContext:
    values = dict(
        direction="bull",
        trend_confirmed=True,
        h1_state="bull_strong",
        m15_slope_state="up",
        m15_reverse_divergence=False,
        gate_open=True,
        m15_index=20,
        current_price=105.0,
        atr14=2.0,
    )
    values.update(overrides)
    return EntryContext(**values)


def test_path_a_requires_any_two_of_three_evidence_items() -> None:
    evidence = EvidenceSnapshot(
        direction="bull", m5_divergence=event("div", 3), m5_pattern=event("pattern", 5)
    )
    settings = EntrySettings(path_b_enabled=False, path_c_enabled=False, path_d_enabled=False)
    decision = evaluate_entries(context(), evidence, settings)
    assert decision.accepted
    assert decision.path == "A"
    assert decision.trigger_bar_id == "bar-5"
    assert decision.evidence_id == "A|bull|bar-5"


def test_simultaneous_paths_select_latest_trigger() -> None:
    evidence = EvidenceSnapshot(
        direction="bull",
        m5_divergence=event("div", 2),
        m5_flatten=event("flat", 4),
        m5_pattern=event("pattern", 8),
        m5_pullback=event("pullback", 6),
    )
    settings = EntrySettings(path_c_enabled=False, path_d_enabled=False)
    decision = evaluate_entries(context(), evidence, settings)
    assert {candidate.path for candidate in decision.candidates} == {"A", "B"}
    assert decision.path == "B"
    assert decision.trigger_bar_id == "bar-8"


def test_path_d_can_stack_twice_and_uses_distance_cooldown_exception() -> None:
    evidence = EvidenceSnapshot(direction="bull", m5_ema_cross=event("cross", 12), m5_last_sequence=12)
    ctx = context(
        trend_confirmed=False,
        same_direction_positions=1,
        last_same_direction_fill_index=19,
        last_same_direction_fill_price=100.0,
    )
    settings = EntrySettings(path_a_enabled=False, path_b_enabled=False, path_c_enabled=False)
    decision = evaluate_entries(ctx, evidence, settings)
    assert decision.accepted
    assert decision.path == "D"


def test_path_d_stack_limit_and_non_d_no_add() -> None:
    d_evidence = EvidenceSnapshot(direction="bull", m5_ema_cross=event("cross", 12), m5_last_sequence=12)
    d_settings = EntrySettings(path_a_enabled=False, path_b_enabled=False, path_c_enabled=False)
    assert evaluate_entries(context(trend_confirmed=False, same_direction_positions=2), d_evidence, d_settings).reason == "PATH_D_STACK_LIMIT"

    a_evidence = EvidenceSnapshot(
        direction="bull", m5_divergence=event("div", 2), m5_flatten=event("flat", 4)
    )
    a_settings = EntrySettings(path_b_enabled=False, path_c_enabled=False, path_d_enabled=False)
    assert evaluate_entries(context(same_direction_positions=1), a_evidence, a_settings).reason == "SAME_DIRECTION_POSITION"
