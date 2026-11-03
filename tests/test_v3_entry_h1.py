"""Regression at the S0 core, experimental ordinary and prepared adapter boundaries."""
from dataclasses import replace

import pytest

import backtest.exit_experiment_strategy as research
from backtest.runner import StepInput
from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.entry import EvidenceEvent, EvidenceSnapshot
from futures_trend_quant.core.entry.paths import entry_h1_reasons
from futures_trend_quant.core.facts.h1_state import relative_h1_state
from futures_trend_quant.core.facts.pipeline import analyze_market
from futures_trend_quant.core.models import TrendState
from futures_trend_quant.core.pipeline import step as core
from futures_trend_quant.core.trend.state_machine import TrendFacts
from helpers import make_bars


@pytest.mark.parametrize("direction,opposite", [("bull", "bear"), ("bear", "bull")])
@pytest.mark.parametrize("stage,path,h1,exception,accepted", [
    ("hold", "B", "opposite_strong", False, False),
    ("hold", "B", "opposite_weakening", False, True),
    ("hold", "B", "opposite_strong", True, True),
    ("establish", "B", "opposite_strong", False, False),
    ("flip", "D", "aligned_strong", False, True),
    ("hold", "D", "opposite_strong", False, False),
    ("flip", "D", "aligned_weakening", False, False),
])
def test_entry_h1_across_real_entry_points(monkeypatch, direction, opposite, stage, path, h1, exception, accepted):
    rules = dict(load_config(default_config_path()).raw["rules"],
                 path_b_h1_exception=exception, anchor_mode="range20", gate_enabled=False)
    absolute = h1.replace("opposite", opposite).replace("aligned", direction)
    history = tuple(make_bars(500))
    last = history[-1]
    event = StepInput(last.symbol, last.eob.date(), last, (), history, None)
    analysis = replace(analyze_market(list(history), rules), h1_absolute=absolute)
    prior = {"hold": direction, "flip": opposite, "establish": "none"}[stage]
    seed = TrendState(trend_state=prior, leave_votes=int(stage == "flip"),
                      establish_direction=direction if stage == "establish" else None,
                      establish_votes=int(stage == "establish"))
    # Fix market observations and M5 evidence. The real trend vote, context
    # construction, H1 gates, path selection, and anchor run in every case.
    facts = TrendFacts(bar_id=last.identity, close=last.close, high=last.high, low=last.low,
                      close_side="above" if direction == "bull" else "below",
                      slope_state="up" if direction == "bull" else "down",
                      r_bullish_reverse=stage == "flip" and direction == "bull",
                      r_bearish_reverse=stage == "flip" and direction == "bear",
                      h1_state=relative_h1_state(absolute, prior if prior != "none" else direction))
    evidence_event = EvidenceEvent("trigger", direction, "trigger-bar", 8)
    evidence = (EvidenceSnapshot(direction, m5_pullback=evidence_event, m5_pattern=evidence_event)
                if path == "B" else EvidenceSnapshot(direction, m5_ema_cross=evidence_event))
    for module in (core, research):
        monkeypatch.setattr(module, "analyze_market", lambda *_: analysis)
        monkeypatch.setattr(module, "trend_facts", lambda *_: facts)
        monkeypatch.setattr(module, "build_entry_evidence", lambda *_, **__: evidence)
    for mode in ("S0", "A", "prepared"):
        adapter = research.ExitExperimentFactsStrategy("S0" if mode == "S0" else "A",
                                                      rules, {last.symbol: last.symbol})
        adapter.runtime.trends[last.symbol] = replace(seed)
        if mode == "prepared":
            adapter.audit_legacy_h1 = True
            output = adapter.run_prepared(event, last.symbol, adapter.runtime, analysis, None, None, {})
        else:
            output = adapter(event)
        row = output.facts
        entry = row["entry_evaluation"]
        assert row["trend_decision"]["prior_state"] == prior
        assert row["trend_decision"]["confirmed_state"] == direction
        assert row["trend_inputs"]["h1_relative"] == facts.h1_state
        assert (entry["h1_absolute"], entry["entry_direction"]) == (absolute, direction)
        assert entry["accepted"] is accepted
        assert entry["path"] == (path if accepted else None)
        if mode == "prepared" and h1 == "opposite_strong" and path == "B":
            assert entry["legacy_h1"]["accepted"] is True
        if mode == "prepared" and h1 == "aligned_strong" and stage == "flip":
            assert entry["legacy_h1"]["accepted"] is False


@pytest.mark.parametrize("legacy", [None, "reverse", "strong", "weakening"])
def test_reading_old_relative_value_cannot_determine_absolute_strength(legacy):
    assert entry_h1_reasons(legacy, "bull", False) == ("H1_ABSOLUTE_UNKNOWN", "H1_ABSOLUTE_UNKNOWN")
