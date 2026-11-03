from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Literal

from ..models import Direction, TrendState


Action = Literal["maintain", "veto", "end_to_none", "flip", "strong_exit", "establish", "stay_none"]


@dataclass(frozen=True)
class TrendFacts:
    bar_id: str
    close: float
    high: float
    low: float
    close_side: Literal["above", "below", "equal", "unavailable"]
    slope_state: Literal["up", "down", "flat", "unavailable"]
    top_divergence: bool = False
    bottom_divergence: bool = False
    s1_extreme_then_flat: bool = False
    s2_bearish_pattern: bool = False
    s2_bullish_pattern: bool = False
    p_close_cross: bool = False
    p_macd_cross: bool = False
    r_bearish_reverse: bool = False
    r_bullish_reverse: bool = False
    h1_state: Literal["strong", "weakening", "flat", "reverse"] = "flat"
    h1_divergence: Literal["none", "pending", "confirmed"] = "none"
    strong_break_atr_met: bool = False
    unblock_allowed: bool = False


@dataclass(frozen=True)
class TrendDecision:
    prior_state: str
    proposed_action: Action
    proposed_state: str
    confirmed: bool
    confirmed_state: str
    end_path: Literal["divergence", "structure"] | None
    reason_code: str
    state: TrendState

    def audit(self) -> dict[str, object]:
        data = asdict(self)
        data["state"] = self.state.to_dict()
        return data


def _opposite(direction: Direction) -> Direction:
    return "bear" if direction == "bull" else "bull"


def _establish_direction(facts: TrendFacts) -> Direction | None:
    bull = facts.close_side == "above" and facts.slope_state == "up" and not facts.top_divergence
    bear = facts.close_side == "below" and facts.slope_state == "down" and not facts.bottom_divergence
    if bull == bear:
        return None
    return "bull" if bull else "bear"


def _leave_proposal(state: TrendState, facts: TrendFacts, veto_enabled: bool) -> tuple[Action, str, str | None]:
    trend = state.trend_state
    if trend == "bull":
        path1 = facts.top_divergence and facts.s1_extreme_then_flat or facts.top_divergence and facts.s2_bearish_pattern
        path1 = path1 and (facts.p_close_cross or facts.p_macd_cross)
        path2 = facts.r_bearish_reverse
        reverse: Direction = "bear"
        strong_override = path1 and facts.s1_extreme_then_flat and facts.s2_bearish_pattern and facts.strong_break_atr_met
    elif trend == "bear":
        path1 = facts.bottom_divergence and facts.s1_extreme_then_flat or facts.bottom_divergence and facts.s2_bullish_pattern
        path1 = path1 and (facts.p_close_cross or facts.p_macd_cross)
        path2 = facts.r_bullish_reverse
        reverse = "bull"
        strong_override = path1 and facts.s1_extreme_then_flat and facts.s2_bullish_pattern and facts.strong_break_atr_met
    else:
        raise ValueError("leave proposal requires an established trend")

    if not path1 and not path2:
        return "maintain", trend, None
    path = "divergence" if path1 else "structure"
    if facts.h1_state == "strong" and veto_enabled:
        if path1 and strong_override:
            return "strong_exit", "none", path
        return "veto", trend, path
    if facts.h1_state == "weakening":
        if facts.h1_divergence in ("pending", "confirmed"):
            return "flip", reverse, path
        return "end_to_none", "none", path
    return "flip", reverse, path


def step_trend(
    current: TrendState,
    facts: TrendFacts,
    *,
    confirm_bars: int = 2,
    veto_enabled: bool = True,
) -> TrendDecision:
    if confirm_bars < 1:
        raise ValueError("confirm_bars must be positive")
    if current.last_closed_bar_id == facts.bar_id:
        raise ValueError("duplicate closed bar")

    state = deepcopy(current)
    prior = state.trend_state
    state.last_closed_bar_id = facts.bar_id

    if prior == "none":
        direction = _establish_direction(facts)
        if direction is None:
            state.establish_votes = 0
            state.establish_direction = None
            return TrendDecision(prior, "stay_none", "none", False, "none", None, "ESTABLISH_CONDITIONS_NOT_MET", state)
        if state.blocked_dir == direction and not facts.unblock_allowed:
            state.establish_votes = 0
            state.establish_direction = None
            return TrendDecision(prior, "stay_none", "none", False, "none", None, "BLOCKED_DIRECTION", state)
        if state.blocked_dir == direction and facts.unblock_allowed:
            state.blocked_dir = None
        if state.establish_direction == direction:
            state.establish_votes += 1
        else:
            state.establish_direction = direction
            state.establish_votes = 1
        confirmed = state.establish_votes >= confirm_bars
        if confirmed:
            state.trend_state = direction
            state.since_bar_id = facts.bar_id
            state.trend_extreme = facts.high if direction == "bull" else facts.low
            state.establish_votes = 0
            state.establish_direction = None
        return TrendDecision(prior, "establish", direction, confirmed, state.trend_state, None, "ESTABLISH_VOTE", state)

    action, proposed_state, end_path = _leave_proposal(state, facts, veto_enabled)
    if prior == "bull":
        state.trend_extreme = max(state.trend_extreme or facts.high, facts.high)
    else:
        state.trend_extreme = min(state.trend_extreme or facts.low, facts.low)

    if action in ("maintain", "veto"):
        state.leave_votes = 0
        return TrendDecision(prior, action, proposed_state, False, prior, end_path, action.upper(), state)

    state.leave_votes += 1
    confirmed = state.leave_votes >= confirm_bars
    if confirmed:
        previous_direction: Direction = prior  # type: ignore[assignment]
        state.trend_state = proposed_state  # type: ignore[assignment]
        state.leave_votes = 0
        state.establish_votes = 0
        state.establish_direction = None
        if action == "strong_exit":
            state.blocked_dir = _opposite(previous_direction)
        if proposed_state == "none":
            state.since_bar_id = None
            state.trend_extreme = None
        else:
            state.since_bar_id = facts.bar_id
            state.trend_extreme = facts.high if proposed_state == "bull" else facts.low
    return TrendDecision(prior, action, proposed_state, confirmed, state.trend_state, end_path, "LEAVE_VOTE", state)
