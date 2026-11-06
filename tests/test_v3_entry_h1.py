"""Regression at the S0 core, experimental ordinary and prepared adapter boundaries."""
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

import backtest.exit_experiment_strategy as research
from backtest.runner import StepInput
from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.entry import EvidenceEvent, EvidenceSnapshot
from futures_trend_quant.core.entry.paths import entry_h1_reasons
from futures_trend_quant.core.facts.h1_state import relative_h1_state
from futures_trend_quant.core.facts.pipeline import analyze_market, finalized_entry_h1
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
    source = history[-8].eob
    analysis = replace(analyze_market(list(history), rules), h1_absolute="flat",
                       finalized_h1_absolute=absolute, finalized_h1_eob=source)
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
        assert entry["h1_source_eob"] == source.isoformat()
        assert row["h1_absolute"] == "flat"
        assert entry["accepted"] is accepted
        assert entry["path"] == (path if accepted else None)
        if mode == "prepared" and h1 == "opposite_strong" and path == "B":
            assert entry["legacy_h1"]["accepted"] is True
        if mode == "prepared" and h1 == "aligned_strong" and stage == "flip":
            assert entry["legacy_h1"]["accepted"] is False


@pytest.mark.parametrize("legacy", [None, "reverse", "strong", "weakening"])
def test_reading_old_relative_value_cannot_determine_absolute_strength(legacy):
    assert entry_h1_reasons(legacy, "bull", False) == ("H1_ABSOLUTE_UNKNOWN", "H1_ABSOLUTE_UNKNOWN")


def _commodity_sessions(step):
    # Actual commodity day-session starts, including both intraday breaks.
    minutes = [9*60 + i*15 for i in range(5)] + [10*60 + 30 + i*15 for i in range(4)]
    minutes += [13*60 + 30 + i*15 for i in range(6)]
    prices = iter(make_bars(22 * len(minutes), start=1000, step=step))
    sessions = []
    day = datetime(2026, 1, 5)
    while len(sessions) < 22:
        if day.weekday() < 5:
            bars = []
            for minute in minutes:
                start = day + timedelta(minutes=minute)
                bars.append(replace(next(prices), bob=start, eob=start+timedelta(minutes=15)))
            sessions.append(bars)
        day += timedelta(days=1)
    return sessions


@pytest.mark.parametrize("step,direction", [(0.2, "bull"), (-0.2, "bear")])
def test_entry_h1_only_uses_finalized_buckets(step, direction):
    rules = dict(load_config(default_config_path()).raw["rules"])
    *prior_days, current, following = _commodity_sessions(step)
    history = [bar for day in prior_days for bar in day]
    previous = finalized_entry_h1(history, rules)
    assert previous == (direction + "_strong", history[-1].eob)
    for count in range(1, 4):
        actual = history + current[:count]
        assert finalized_entry_h1(actual, rules) == previous
        last = actual[-1]
        price = last.close - 300 if direction == "bull" else last.close + 300
        shocked = [*actual[:-1], replace(last, high=max(last.high, price), low=min(last.low, price), close=price)]
        analysis = analyze_market(shocked, rules)
        assert (analysis.finalized_h1_absolute, analysis.finalized_h1_eob) == previous
        assert analysis.h1_absolute == previous[0]
    for count in range(4, 15):
        expected = current[max(i for i in (3,4,8,12) if i<count)].eob
        assert finalized_entry_h1(history + current[:count], rules)[1] == expected
    day_end = analyze_market(history + current, rules)
    assert day_end.finalized_h1_eob == current[-1].eob
    assert day_end.finalized_h1_absolute == day_end.h1_absolute
    assert finalized_entry_h1(history + current + following[:1], rules) == (day_end.h1_absolute, current[-1].eob)


@pytest.mark.parametrize("days_to_next,opening_hour", [(0, 21), (3, 9), (10, 9)])
def test_missing_friday_close_waits_for_next_observed_trading_day(days_to_next, opening_hour):
    rules = dict(load_config(default_config_path()).raw["rules"])
    sessions = _commodity_sessions(0.2)
    friday = sessions[19]
    assert friday[-1].eob.weekday() == 4
    history = [bar for day in sessions[:19] for bar in day] + friday[:-1]
    assert finalized_entry_h1(history, rules)[1] == friday[12].eob
    # A later session cannot turn a bucket with a missing source into complete H1.
    bob = friday[-1].eob.replace(hour=opening_hour, minute=0) + timedelta(days=days_to_next)
    next_bar = replace(sessions[20][0], bob=bob, eob=bob + timedelta(minutes=15))
    assert finalized_entry_h1(history + [next_bar], rules)[1] == friday[12].eob


def test_unavailable_closed_h1_does_not_block_historical_trend_facts():
    rules = dict(load_config(default_config_path()).raw["rules"])
    *days, last = _commodity_sessions(0.2)
    flat = [replace(bar, open=1000, close=1000, high=1000, low=1000, pre_close=1000)
            for day in days for bar in day]
    evolving = replace(last[0], open=1000, close=1002, high=1002, low=1000, pre_close=1000)
    with pytest.raises(ValueError,match='positive'):
        analyze_market(flat + [evolving], rules)
