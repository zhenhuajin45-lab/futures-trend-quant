from __future__ import annotations

from copy import deepcopy
from typing import Literal, cast

from futures_trend_quant.core.models import Direction, TrendState
from futures_trend_quant.core.trend.state_machine import TrendDecision, TrendFacts, step_trend


ExperimentGroup = Literal["S0", "EMA34_CONTROL", "A", "B"]


def _opposite(direction: Direction) -> Direction:
    return "bear" if direction == "bull" else "bull"


def _directional_inputs(state: TrendState, facts: TrendFacts) -> tuple[bool, bool, bool, bool]:
    if state.trend_state == "bull":
        divergence = facts.top_divergence
        pattern = facts.s2_bearish_pattern
        reverse = facts.r_bearish_reverse
    elif state.trend_state == "bear":
        divergence = facts.bottom_divergence
        pattern = facts.s2_bullish_pattern
        reverse = facts.r_bullish_reverse
    else:
        raise ValueError("directional exit inputs require an established trend")
    p_any = facts.p_close_cross or facts.p_macd_cross
    original_path1 = divergence and (facts.s1_extreme_then_flat or pattern) and p_any
    return original_path1, reverse, pattern, p_any


def _candidate_decision(
    current: TrendState,
    facts: TrendFacts,
    *,
    action: Literal["veto", "end_to_none", "flip"],
    proposed_state: str,
    reason_code: str,
    confirm_bars: int,
) -> TrendDecision:
    state = deepcopy(current)
    prior = state.trend_state
    state.last_closed_bar_id = facts.bar_id
    if prior == "bull":
        state.trend_extreme = max(state.trend_extreme or facts.high, facts.high)
    elif prior == "bear":
        state.trend_extreme = min(state.trend_extreme or facts.low, facts.low)
    else:
        raise ValueError("candidate exit requires an established trend")
    if action == "veto":
        state.leave_votes = 0
        return TrendDecision(prior, action, prior, False, prior, None, reason_code, state)

    state.leave_votes += 1
    confirmed = state.leave_votes >= confirm_bars
    if confirmed:
        state.trend_state = cast(object, proposed_state)  # runtime value is validated below
        if state.trend_state not in ("none", "bull", "bear"):
            raise ValueError("unsupported candidate state")
        state.leave_votes = 0
        state.establish_votes = 0
        state.establish_direction = None
        if proposed_state == "none":
            state.since_bar_id = None
            state.trend_extreme = None
        else:
            state.since_bar_id = facts.bar_id
            state.trend_extreme = facts.high if proposed_state == "bull" else facts.low
    return TrendDecision(
        prior,
        action,
        proposed_state,
        confirmed,
        state.trend_state,
        None,
        reason_code,
        state,
    )


def _step_ema34_control(current: TrendState, facts: TrendFacts, confirm_bars: int) -> TrendDecision:
    if current.last_closed_bar_id == facts.bar_id:
        raise ValueError("duplicate closed bar")
    state = deepcopy(current)
    prior = state.trend_state
    state.last_closed_bar_id = facts.bar_id
    candidate: Direction | None = (
        "bull" if facts.close_side == "above" else "bear" if facts.close_side == "below" else None
    )
    if candidate is None or candidate == prior:
        state.establish_votes = 0
        state.establish_direction = None
        action = "maintain" if prior != "none" else "stay_none"
        return TrendDecision(prior, action, prior, False, prior, None, "EMA34_HOLD", state)
    if state.establish_direction == candidate:
        state.establish_votes += 1
    else:
        state.establish_direction = candidate
        state.establish_votes = 1
    confirmed = state.establish_votes >= confirm_bars
    action = "establish" if prior == "none" else "flip"
    if confirmed:
        state.trend_state = candidate
        state.since_bar_id = facts.bar_id
        state.trend_extreme = facts.high if candidate == "bull" else facts.low
        state.establish_votes = 0
        state.establish_direction = None
        state.leave_votes = 0
    return TrendDecision(
        prior,
        action,
        candidate,
        confirmed,
        state.trend_state,
        None,
        "EMA34_TWO_CLOSE_VOTE",
        state,
    )


def step_experiment_trend(
    current: TrendState,
    facts: TrendFacts,
    *,
    group: ExperimentGroup,
    confirm_bars: int = 2,
    veto_enabled: bool = True,
) -> TrendDecision:
    """Research-only trend step; S0 delegates to the production state machine."""

    if confirm_bars < 1:
        raise ValueError("confirm_bars must be positive")
    if group == "S0":
        return step_trend(current, facts, confirm_bars=confirm_bars, veto_enabled=veto_enabled)
    if group == "EMA34_CONTROL":
        return _step_ema34_control(current, facts, confirm_bars)
    if current.trend_state == "none":
        return step_trend(current, facts, confirm_bars=confirm_bars, veto_enabled=veto_enabled)

    original_path1, original_path2, pattern, p_any = _directional_inputs(current, facts)
    if original_path1 or original_path2:
        return step_trend(current, facts, confirm_bars=confirm_bars, veto_enabled=veto_enabled)

    reverse = _opposite(cast(Direction, current.trend_state))
    if group == "A":
        proposal = (facts.s1_extreme_then_flat or pattern) and p_any
        if not proposal:
            return step_trend(current, facts, confirm_bars=confirm_bars, veto_enabled=veto_enabled)
        if facts.h1_state == "strong" and veto_enabled:
            return _candidate_decision(
                current,
                facts,
                action="veto",
                proposed_state=current.trend_state,
                reason_code="A_H1_STRONG_VETO",
                confirm_bars=confirm_bars,
            )
        if facts.h1_state == "weakening" and facts.h1_divergence not in ("pending", "confirmed"):
            action, proposed = "end_to_none", "none"
        else:
            action, proposed = "flip", reverse
        return _candidate_decision(
            current,
            facts,
            action=action,
            proposed_state=proposed,
            reason_code="A_RELAX_DIVERGENCE_VOTE",
            confirm_bars=confirm_bars,
        )

    if group == "B":
        proposal = pattern and p_any and facts.h1_state in ("weakening", "flat")
        if not proposal:
            return step_trend(current, facts, confirm_bars=confirm_bars, veto_enabled=veto_enabled)
        return _candidate_decision(
            current,
            facts,
            action="end_to_none",
            proposed_state="none",
            reason_code="B_END_TO_NONE_VOTE",
            confirm_bars=confirm_bars,
        )
    raise ValueError(f"unsupported experiment group: {group}")
