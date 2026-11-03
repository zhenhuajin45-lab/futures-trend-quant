from backtest.exit_experiment_strategy import ExitExperimentFactsStrategy
from backtest.facts_runner import FactsOnlyRunner
from backtest.smoke import synthetic_events
from backtest.strategy import SharedCoreFactsStrategy
from futures_trend_quant.config import default_config_path, load_config


def test_s0_research_adapter_matches_shared_core_before_metadata() -> None:
    config = load_config(default_config_path())
    symbol = "SHFE.AUSYNTH"
    mapping = {symbol: "SHFE.AU"}
    baseline = FactsOnlyRunner(
        strategy_step=SharedCoreFactsStrategy(config.raw["rules"], mapping)
    ).run(synthetic_events())
    research = FactsOnlyRunner(
        strategy_step=ExitExperimentFactsStrategy("S0", config.raw["rules"], mapping)
    ).run(synthetic_events())
    baseline_rows = [dict(row.values) for row in baseline.facts if "trend_decision" in row.values]
    research_rows = [dict(row.values) for row in research.facts if "trend_decision" in row.values]
    assert len(baseline_rows) == len(research_rows) == 50
    extra = {"high", "low", "experiment_group", "entry_evaluation"}
    assert [
        {key: value for key, value in row.items() if key not in extra}
        for row in research_rows
    ] == baseline_rows


def test_all_groups_are_facts_only_and_use_independent_runtime() -> None:
    config = load_config(default_config_path())
    symbol = "SHFE.AUSYNTH"
    strategies = [
        ExitExperimentFactsStrategy(group, config.raw["rules"], {symbol: "SHFE.AU"})
        for group in ("S0", "EMA34_CONTROL", "A", "B")
    ]
    results = [FactsOnlyRunner(strategy_step=item).run(synthetic_events()) for item in strategies]
    assert all(len([row for row in result.facts if "trend_decision" in row.values]) == 50 for result in results)
    assert len({id(item.runtime) for item in strategies}) == 4
    assert all(result.facts[-1].values["experiment_group"] == group for result, group in zip(
        results, ("S0", "EMA34_CONTROL", "A", "B")
    ))
