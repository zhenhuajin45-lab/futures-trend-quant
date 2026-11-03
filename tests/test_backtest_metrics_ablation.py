from datetime import date, datetime, timedelta

from backtest.ablation import ablation_support, generate_ablation_cases, generate_sprint2_cases
from backtest.metrics import blocked_distribution, score_preregistered_trend, trend_layer_metrics
from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.models import Bar


def test_ablation_changes_one_boolean_or_declared_enum_at_a_time() -> None:
    baseline = {"rules": {"gate": True, "mode": "strict", "threshold": 3}}
    cases = generate_ablation_cases(
        baseline, domains={"rules.mode": ("strict", "loose")}
    )
    assert {(case.key_path, case.candidate) for case in cases} == {
        ("rules.gate", False),
        ("rules.mode", "loose"),
    }
    assert all(case.config["rules"]["threshold"] == 3 for case in cases)


def test_metrics_include_blocked_distribution_and_four_trend_fields() -> None:
    assert blocked_distribution(["GATE", "GATE", "COOLDOWN"]) == {"COOLDOWN": 1, "GATE": 2}
    metrics = trend_layer_metrics([1, 1, -1, 0], [1, -1, -1, 0])
    assert set(metrics) == {
        "evaluated_bars",
        "directional_recall",
        "directional_precision",
        "coverage",
        "flip_rate",
    }


def _bars(closes: list[float]) -> list[Bar]:
    start = datetime(2026, 1, 5, 9)
    return [
        Bar(
            symbol="TEST.X",
            frequency="900s",
            bob=start + timedelta(minutes=15 * index),
            eob=start + timedelta(minutes=15 * (index + 1)),
            open=close,
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            volume=1,
        )
        for index, close in enumerate(closes)
    ]


def test_preregistered_trend_metrics_follow_frozen_denominators() -> None:
    bars = _bars([100, 101, 102, 103, 102, 101, 100, 99])
    metrics = score_preregistered_trend(
        bars,
        atr=[1.0] * 8,
        predicted=["bull", "bull", "bull", "bull", "bull", "bear", "bear", "bear"],
        reference=[1, 1, 1, 0, 0, -1, -1, -1],
        trading_dates=[date(2026, 1, 5)] * 8,
    )
    assert metrics["direction_agreement"] == 1.0
    assert metrics["eligible_trend_segments"] == 2
    assert metrics["trend_segment_coverage"] == 1.0
    assert metrics["range_switches"] == 0
    assert metrics["turn_delay_median"] == 0.0
    assert metrics["turn_delay_passed"] is True


def test_turn_delay_unresolved_is_penalized_as_49_bars() -> None:
    bars = _bars([100.0] * 6)
    metrics = score_preregistered_trend(
        bars,
        atr=[1.0] * 6,
        predicted=["bull"] * 6,
        reference=[1, 1, -1, -1, -1, -1],
        trading_dates=[date(2026, 1, 5)] * 6,
    )
    assert metrics["turn_events"] == 1
    assert metrics["turn_delay_median"] == 49.0
    assert metrics["unresolved_turn_rate"] == 1.0
    assert metrics["turn_delay_passed"] is False


def test_pool_scoring_does_not_create_turns_or_segments_across_symbols() -> None:
    bars = _bars([100, 101, 102, 100, 99, 98])
    metrics = score_preregistered_trend(
        bars,
        atr=[1.0] * 6,
        predicted=["bull", "bull", "bull", "bear", "bear", "bear"],
        reference=[1, 1, 1, -1, -1, -1],
        trading_dates=[date(2026, 1, 5)] * 6,
        symbols=["A", "A", "A", "B", "B", "B"],
    )
    assert metrics["eligible_trend_segments"] == 2
    assert metrics["turn_events"] == 0


def test_sprint2_ablation_matrix_is_frozen_and_flags_unimplemented_pm() -> None:
    baseline = load_config(default_config_path()).raw
    cases = generate_sprint2_cases(baseline)
    names = {case.name for case in cases}
    assert "rules.h1_strong_atr=0.4" in names
    assert "rules.h1_strong_atr=0.8" in names
    assert "rules.pm_mode=be_at_1r" in names
    pm_case = next(case for case in cases if case.name == "rules.pm_mode=be_at_1r")
    supported, reason = ablation_support(pm_case)
    assert supported is False
    assert "not implemented" in reason
    for case in cases:
        changed = [
            key
            for key, value in case.config["rules"].items()
            if value != baseline["rules"][key]
        ]
        assert changed == [case.key_path.split(".")[-1]]
