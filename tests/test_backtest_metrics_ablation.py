from backtest.ablation import generate_ablation_cases
from backtest.metrics import blocked_distribution, trend_layer_metrics


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
