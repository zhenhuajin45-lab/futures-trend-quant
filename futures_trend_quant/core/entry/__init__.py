from __future__ import annotations

from ..models import Bar
from .anchor import AnchorMode, AnchorSelection, select_anchor
from .cooldown import CooldownDecision, evaluate_cooldown
from .evidence import (
    build_entry_evidence,
    find_m1_confirmation,
    m1_strong_reverse,
    recent_m5_event,
)
from .gate import GateDecision, evaluate_momentum_gate
from .models import (
    Direction,
    EntryContext,
    EntryDecision,
    EntryEvidence,
    EntrySettings,
    EvidenceEvent,
    EvidenceSnapshot,
    PathCandidate,
    PathName,
)
from .paths import (
    evaluate_entries,
    evaluate_path_a,
    evaluate_path_b,
    evaluate_path_c,
    evaluate_path_d_candidate,
)


# These names intentionally remain module globals: old tests and GM adapters
# patch them directly while migrating to evaluate_entries().
_recent_m5_event = recent_m5_event
_m1_strong_reverse = m1_strong_reverse


def evaluate_path_d(
    *,
    direction: Direction,
    bar_id: str,
    h1_state: str,
    m15_slope_state: str,
    reverse_divergence: bool,
    m5_bars: list[Bar],
    m1_bars: list[Bar],
) -> EntryEvidence:
    """Backward-compatible facade for the original GM-only Path-D call."""

    evidence_id = f"path_d|{direction}|{bar_id}"
    if h1_state != "strong":
        return EntryEvidence(False, direction, evidence_id, "H1_NOT_STRONG", None, False)
    required_slope = "up" if direction == "bull" else "down"
    if m15_slope_state != required_slope:
        return EntryEvidence(False, direction, evidence_id, "M15_SLOPE_MISMATCH", None, False)
    if reverse_divergence:
        return EntryEvidence(False, direction, evidence_id, "REVERSE_DIVERGENCE", None, False)
    event = _recent_m5_event(m5_bars, direction)
    if event is None:
        return EntryEvidence(False, direction, evidence_id, "M5_TRIGGER_MISSING", None, False)
    evidence_id = f"path_d|{direction}|{event}"
    reverse = _m1_strong_reverse(m1_bars, direction)
    if reverse:
        return EntryEvidence(False, direction, evidence_id, "M1_STRONG_REVERSE", event, True)
    return EntryEvidence(True, direction, evidence_id, "PATH_D_ACCEPTED", event, False)


__all__ = [
    "AnchorMode",
    "AnchorSelection",
    "CooldownDecision",
    "Direction",
    "EntryContext",
    "EntryDecision",
    "EntryEvidence",
    "EntrySettings",
    "EvidenceEvent",
    "EvidenceSnapshot",
    "GateDecision",
    "PathCandidate",
    "PathName",
    "build_entry_evidence",
    "evaluate_cooldown",
    "evaluate_entries",
    "evaluate_momentum_gate",
    "evaluate_path_a",
    "evaluate_path_b",
    "evaluate_path_c",
    "evaluate_path_d",
    "evaluate_path_d_candidate",
    "find_m1_confirmation",
    "select_anchor",
]
