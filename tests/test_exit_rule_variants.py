from dataclasses import replace

from backtest.exit_rule_variants import step_experiment_trend
from futures_trend_quant.core.models import TrendState
from futures_trend_quant.core.trend.state_machine import TrendFacts, step_trend


def _facts(bar: int, **changes) -> TrendFacts:
    base = TrendFacts(
        bar_id=f"bar-{bar}",
        close=100,
        high=101,
        low=99,
        close_side="below",
        slope_state="flat",
        s1_extreme_then_flat=True,
        p_close_cross=True,
        h1_state="flat",
    )
    return replace(base, **changes)


def _state(direction: str = "bull") -> TrendState:
    return TrendState(trend_state=direction, trend_extreme=100, since_bar_id="start")


def test_s0_is_exact_production_delegate_before_any_divergence() -> None:
    facts = _facts(1, s1_extreme_then_flat=False, p_close_cross=False)
    assert step_experiment_trend(_state(), facts, group="S0") == step_trend(_state(), facts)
    assert step_experiment_trend(_state(), facts, group="A") == step_trend(_state(), facts)
    assert step_experiment_trend(_state(), facts, group="B") == step_trend(_state(), facts)


def test_a_is_bull_bear_mirrored_and_requires_two_votes() -> None:
    first_bull = step_experiment_trend(_state("bull"), _facts(1), group="A")
    second_bull = step_experiment_trend(first_bull.state, _facts(2), group="A")
    assert first_bull.confirmed is False
    assert second_bull.confirmed_state == "bear"

    bear_facts = _facts(
        1,
        close_side="above",
        s2_bullish_pattern=True,
        s1_extreme_then_flat=False,
    )
    first_bear = step_experiment_trend(_state("bear"), bear_facts, group="A")
    second_bear = step_experiment_trend(
        first_bear.state,
        replace(bear_facts, bar_id="bar-2"),
        group="A",
    )
    assert second_bear.confirmed_state == "bull"


def test_original_path_has_priority_and_a_strong_h1_vetoes_without_override() -> None:
    original = _facts(1, top_divergence=True)
    assert step_experiment_trend(_state(), original, group="A") == step_trend(_state(), original)

    strong = _facts(
        2,
        h1_state="strong",
        s2_bearish_pattern=True,
        strong_break_atr_met=True,
    )
    decision = step_experiment_trend(_state(), strong, group="A")
    assert decision.proposed_action == "veto"
    assert decision.reason_code == "A_H1_STRONG_VETO"
    assert decision.state.leave_votes == 0


def test_candidate_vote_is_reset_by_maintain() -> None:
    first = step_experiment_trend(_state(), _facts(1), group="A")
    interrupted = step_experiment_trend(
        first.state,
        _facts(2, s1_extreme_then_flat=False, p_close_cross=False),
        group="A",
    )
    assert first.state.leave_votes == 1
    assert interrupted.proposed_action == "maintain"
    assert interrupted.state.leave_votes == 0


def test_b_only_ends_to_none_then_original_establishment_rebuilds() -> None:
    trigger = _facts(1, s1_extreme_then_flat=False, s2_bearish_pattern=True, h1_state="flat")
    first = step_experiment_trend(_state(), trigger, group="B")
    second = step_experiment_trend(
        first.state,
        replace(trigger, bar_id="bar-2"),
        group="B",
    )
    assert second.confirmed is True
    assert second.confirmed_state == "none"
    assert second.reason_code == "B_END_TO_NONE_VOTE"

    establish = _facts(
        3,
        close_side="below",
        slope_state="down",
        s1_extreme_then_flat=False,
        s2_bearish_pattern=False,
        p_close_cross=False,
    )
    vote = step_experiment_trend(second.state, establish, group="B")
    confirmed = step_experiment_trend(vote.state, replace(establish, bar_id="bar-4"), group="B")
    assert confirmed.confirmed_state == "bear"
