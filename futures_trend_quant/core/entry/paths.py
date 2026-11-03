from __future__ import annotations

from ..facts.h1_state import H1Absolute, relative_h1_state
from typing import cast, get_args

from .cooldown import evaluate_cooldown
from .models import (
    Direction,
    EntryContext,
    EntryDecision,
    EntrySettings,
    EvidenceEvent,
    EvidenceSnapshot,
    PathCandidate,
    PathName,
)


def _completion_event(events: list[EvidenceEvent], required: int | None = None) -> EvidenceEvent | None:
    if required is not None and len(events) < required:
        return None
    if not events:
        return None
    ordered = sorted(events, key=lambda event: event.sequence)
    # For a 2-of-3 rule, the middle event is when the rule first became true.
    return ordered[required - 1] if required is not None else ordered[-1]


def _candidate(path: PathName, direction: Direction, event: EvidenceEvent | None) -> PathCandidate | None:
    if event is None:
        return None
    return PathCandidate(path, direction, event.bar_id, event.sequence, event.eob)


def evaluate_path_a(
    context: EntryContext, evidence: EvidenceSnapshot, settings: EntrySettings
) -> PathCandidate | None:
    if not settings.path_a_enabled or not context.trend_confirmed:
        return None
    event = _completion_event(
        [event for event in (evidence.m5_divergence, evidence.m5_flatten, evidence.m5_pattern) if event],
        required=2,
    )
    return _candidate("A", context.direction, event)


_ABSOLUTE_STATES = frozenset(get_args(H1Absolute))


def entry_h1_reasons(absolute: str | None, direction: Direction, allow_reverse: bool) -> tuple[str, str]:
    """Return Path B and D H1 gate reasons using the confirmed entry direction."""
    if absolute not in _ABSOLUTE_STATES:
        return "H1_ABSOLUTE_UNKNOWN", "H1_ABSOLUTE_UNKNOWN"
    aligned = relative_h1_state(cast(H1Absolute, absolute), direction)
    opposite: Direction = "bear" if direction == "bull" else "bull"
    counter = relative_h1_state(cast(H1Absolute, absolute), opposite)
    b = "H1_REVERSE_STRONG" if counter == "strong" and not allow_reverse else "PASS"
    d = "PASS" if aligned == "strong" else "H1_NOT_ALIGNED_STRONG"
    return b, d


def evaluate_path_b(
    context: EntryContext, evidence: EvidenceSnapshot, settings: EntrySettings
) -> PathCandidate | None:
    if not settings.path_b_enabled or not context.trend_confirmed:
        return None
    if entry_h1_reasons(context.h1_state, context.direction, settings.path_b_h1_exception)[0] != "PASS":
        return None
    if evidence.m5_reverse_divergence:
        return None
    if evidence.m5_pullback is None or evidence.m5_pattern is None:
        return None
    return _candidate(
        "B", context.direction, _completion_event([evidence.m5_pullback, evidence.m5_pattern])
    )


def evaluate_path_c(
    context: EntryContext, evidence: EvidenceSnapshot, settings: EntrySettings
) -> PathCandidate | None:
    if not settings.path_c_enabled or not context.trend_confirmed or context.same_direction_positions:
        return None
    if evidence.m5_consolidation is None:
        return None
    confirmation = evidence.m5_breakout or evidence.m5_pattern
    if confirmation is None:
        return None
    return _candidate(
        "C", context.direction, _completion_event([evidence.m5_consolidation, confirmation])
    )


def evaluate_path_d_candidate(
    context: EntryContext, evidence: EvidenceSnapshot, settings: EntrySettings
) -> PathCandidate | None:
    required_slope = "up" if context.direction == "bull" else "down"
    if (
        not settings.path_d_enabled
        or entry_h1_reasons(context.h1_state, context.direction, settings.path_b_h1_exception)[1] != "PASS"
        or context.m15_slope_state != required_slope
        or context.m15_reverse_divergence
        or not context.gate_open
        or evidence.m1_strong_reverse
    ):
        return None
    events = [event for event in (evidence.m5_divergence, evidence.m5_ema_cross, evidence.m5_engulfing) if event]
    if evidence.m5_last_sequence is not None:
        events = [event for event in events if evidence.m5_last_sequence - event.sequence <= 5]
    event = _completion_event(events)
    return _candidate("D", context.direction, event)


def evaluate_entries(
    context: EntryContext,
    evidence: EvidenceSnapshot,
    settings: EntrySettings | None = None,
) -> EntryDecision:
    """Evaluate A/B/C/D and select the path with the latest trigger bar.

    This is the shared pure entry point intended for both GM and the offline
    event runner.  Position, uniqueness and cooldown checks are deliberately
    applied after path formation so rejected facts remain observable.
    """

    settings = settings or EntrySettings()
    if evidence.direction != context.direction:
        raise ValueError("evidence direction does not match entry context")
    raw = (
        evaluate_path_a(context, evidence, settings),
        evaluate_path_b(context, evidence, settings),
        evaluate_path_c(context, evidence, settings),
        evaluate_path_d_candidate(context, evidence, settings),
    )
    candidates = tuple(candidate for candidate in raw if candidate is not None)
    if not candidates:
        return EntryDecision(False, context.direction, "NO_PATH")

    # M1 confirmation is an optional global entry gate. Path D always retains
    # its mandatory strong-reverse veto above.
    if settings.m1_confirmation_enabled and evidence.m1_confirmation is None:
        return EntryDecision(False, context.direction, "M1_CONFIRMATION_MISSING", candidates=candidates)

    # max() is stable, so an exact tie keeps path priority A, B, C, D.
    selected = max(candidates, key=lambda candidate: candidate.trigger_sequence)
    if (
        settings.m1_confirmation_enabled
        and selected.trigger_eob is not None
        and evidence.m1_confirmation is not None
        and evidence.m1_confirmation.eob is not None
        and evidence.m1_confirmation.eob <= selected.trigger_eob
    ):
        return EntryDecision(False, context.direction, "M1_CONFIRMATION_BEFORE_TRIGGER", candidates=candidates)
    if selected.evidence_id in context.used_evidence_ids:
        return EntryDecision(
            False, context.direction, "EVIDENCE_ALREADY_USED", selected.path,
            selected.trigger_bar_id, selected.evidence_id, candidates,
        )

    if selected.path == "D":
        if context.same_direction_positions >= settings.path_d_max_positions:
            return EntryDecision(
                False, context.direction, "PATH_D_STACK_LIMIT", selected.path,
                selected.trigger_bar_id, selected.evidence_id, candidates,
            )
    elif context.same_direction_positions:
        return EntryDecision(
            False, context.direction, "SAME_DIRECTION_POSITION", selected.path,
            selected.trigger_bar_id, selected.evidence_id, candidates,
        )

    if settings.cooldown_enabled:
        cooldown = evaluate_cooldown(
            path=selected.path,
            direction=context.direction,
            current_index=context.m15_index,
            current_price=context.current_price,
            atr14=context.atr14,
            last_fill_index=context.last_same_direction_fill_index,
            last_fill_price=context.last_same_direction_fill_price,
            cooldown_bars=settings.cooldown_bars,
            path_d_exception_atr=settings.path_d_add_distance_atr,
        )
        if not cooldown.allowed:
            return EntryDecision(
                False, context.direction, cooldown.reason, selected.path,
                selected.trigger_bar_id, selected.evidence_id, candidates,
            )
    return EntryDecision(
        True, context.direction, f"PATH_{selected.path}_ACCEPTED", selected.path,
        selected.trigger_bar_id, selected.evidence_id, candidates,
    )
