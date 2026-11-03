from __future__ import annotations

from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.pipeline import CoreStepInput, StrategyRuntimeBook, run_core_step
from helpers import make_bars


def test_shared_core_step_is_deterministic_and_runtime_roundtrips() -> None:
    bars = tuple(make_bars(1200, step=0.02))
    rules = load_config(default_config_path()).raw["rules"]
    event = CoreStepInput(
        csymbol="TEST.X",
        symbol=bars[-1].symbol,
        m15_bars=bars,
        m5_bars=bars,
        m1_bars=bars,
        rules=rules,
    )
    left_runtime = StrategyRuntimeBook()
    right_runtime = StrategyRuntimeBook.from_dict(left_runtime.to_dict())
    left = run_core_step(event, left_runtime)
    right = run_core_step(event, right_runtime)
    assert left.facts_row == right.facts_row
    assert left.trend.audit() == right.trend.audit()
    assert left.entry == right.entry
    assert StrategyRuntimeBook.from_dict(left_runtime.to_dict()).to_dict() == left_runtime.to_dict()


def test_fill_state_persists_exact_cooldown_fields() -> None:
    runtime = StrategyRuntimeBook()
    runtime.m15_indices["SHFE.AU"] = 25
    runtime.record_fill(
        csymbol="SHFE.AU",
        direction="bull",
        price=850.2,
        evidence_id="D|bull|bar-1",
        m15_index=24,
    )
    restored = StrategyRuntimeBook.from_dict(runtime.to_dict())
    assert restored.last_fill_indices["SHFE.AU|bull"] == 24
    assert restored.last_fill_prices["SHFE.AU|bull"] == 850.2
    assert restored.used_evidence_ids == {"D|bull|bar-1"}
