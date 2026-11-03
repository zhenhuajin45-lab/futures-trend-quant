from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean, median
from typing import Iterable, Mapping, Sequence

import pandas as pd

from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.facts.pipeline import analyze_market, trading_day_for_bar
from futures_trend_quant.core.facts_state import (
    FactsStateBook,
    latest_valid_divergence,
    update_facts_state,
)
from futures_trend_quant.core.models import Bar
from futures_trend_quant.core.pipeline import StrategyRuntimeBook
from futures_trend_quant.data.development_export import validate_dataset_manifest
from futures_trend_quant.data.gm_bars import bars_from_records

from .aggregation import SequentialAggregator
from .exit_experiment_strategy import ExitExperimentFactsStrategy
from .exit_rule_variants import ExperimentGroup
from .leg_scoring import (
    public_boundary_counts,
    score_reference_leg_events,
    summarize_leg_event_details,
)
from .metrics import future_48bar_3atr_first_touch, score_legacy_first_touch_metrics
from .reference_events import (
    atr_directional_change_reference,
    score_confirmed_turn_events,
    summarize_scored_event_details,
)
from .runner import ReplayBar, StepInput
from .trend_report import _eob_key, _predicted_state


GROUPS: tuple[ExperimentGroup, ...] = ("S0", "EMA34_CONTROL", "A", "B")
SAMPLE_START = date(2026, 5, 7)
SAMPLE_END = date(2026, 11, 2)
BASE_SHA = "aa5f2fe92f6e79b9d8c9f9051a5ea2043356318b"
EXPECTED_BOUNDARY_COUNTS = {
    "scoreable": 793,
    "overlapping_windows": 613,
    "original_resolved": 630,
    "late_credit": 42,
    "resolved_in_leg": 588,
    "within_4_bars": 376,
}
BAR_KEYS = (
    "evaluated_bars",
    "directional_reference_bars",
    "direction_agreement",
    "direction_agreement_passed",
    "eligible_trend_segments",
    "trend_segment_bars",
    "trend_segment_coverage",
    "trend_segment_coverage_p25",
    "trend_segment_coverage_median",
    "trend_segment_coverage_p75",
    "trend_segment_coverage_passed",
    "range_product_days",
    "range_switches",
    "range_switches_per_product_day",
    "range_switches_p95",
    "range_switches_max",
    "range_switches_passed",
)


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_product(
    root_text: str,
    continuous_path: str,
    csymbol: str,
    rules: dict[str, object],
    history_limit: int,
    groups: tuple[ExperimentGroup, ...],
    sample_start: date = SAMPLE_START,
    sample_end: date = SAMPLE_END,
    *,
    strategy_factory=ExitExperimentFactsStrategy,
) -> dict[str, object]:
    root = Path(root_text)
    frame = pd.read_parquet(root / continuous_path)
    if "trading_date" not in frame.columns:
        raise ValueError(f"continuous data for {csymbol} lacks trading_date")
    records = frame.to_dict("records")
    bars = bars_from_records(records)
    if {bar.symbol for bar in bars} != {csymbol}:
        raise ValueError(f"continuous data identity mismatch for {csymbol}")
    events = [
        ReplayBar(bar, date.fromisoformat(str(raw["trading_date"])))
        for bar, raw in zip(bars, records)
    ]
    strategies = {
        group: strategy_factory(
            group=group,
            rules=rules,
            csymbol_by_symbol={csymbol: csymbol},
        )
        for group in groups
    }
    shared_facts = FactsStateBook()
    m5_agg = SequentialAggregator(5, "300s")
    m15_agg = SequentialAggregator(15, "900s")
    m1_history: list[Bar] = []
    m5_history: list[Bar] = []
    m15_history: list[Bar] = []
    group_rows: dict[str, list[dict[str, object]]] = {group: [] for group in groups}
    selected_bars: list[Bar] = []
    selected_dates: list[date] = []
    warmup_evidence = {group: set() for group in groups}
    last_eob = None
    for replay_event in events:
        bar = replay_event.bar
        if last_eob is not None and bar.eob <= last_eob:
            raise ValueError(f"non-increasing bars for {csymbol}")
        last_eob = bar.eob
        m1_history.append(bar)
        del m1_history[:-history_limit]
        m5_closed = m5_agg.update(bar)
        if m5_closed is not None:
            m5_history.append(m5_closed)
            del m5_history[:-history_limit]
        m15_closed = m15_agg.update(bar)
        if m15_closed is None:
            continue
        m15_history.append(m15_closed)
        del m15_history[:-history_limit]
        step_input = StepInput(
            symbol=csymbol,
            trading_date=replay_event.trading_date,
            m15_bar=m15_closed,
            m5_bars=tuple(m5_history),
            m15_bars=tuple(m15_history),
            position=None,
            m1_bars=tuple(m1_history),
            positions=(),
        )
        candidate_facts = FactsStateBook.from_dict(shared_facts.to_dict())
        facts_state = candidate_facts.for_csymbol(csymbol)
        try:
            update_facts_state(facts_state, list(step_input.m15_bars), rules)
            analysis = analyze_market(list(step_input.m15_bars), rules)
        except ValueError as exc:
            message = str(exc).lower()
            if any(marker in message for marker in ("warmup", "unavailable", "requires")):
                continue
            raise
        stable_top = latest_valid_divergence(facts_state, list(step_input.m15_bars), "top")
        stable_bottom = latest_valid_divergence(facts_state, list(step_input.m15_bars), "bottom")
        shared_cache: dict[object, object] = {}
        bar_outputs: dict[str, dict[str, object]] = {}
        candidate_runtimes: dict[str, StrategyRuntimeBook] = {}
        for group, strategy in strategies.items():
            runtime = StrategyRuntimeBook.from_dict(strategy.runtime.to_dict())
            result = strategy.run_prepared(
                step_input,
                csymbol,
                runtime,
                analysis,
                stable_top,
                stable_bottom,
                shared_cache,
            )
            bar_outputs[group] = dict(result.facts)
            candidate_runtimes[group] = runtime
        shared_facts = candidate_facts
        for group, runtime in candidate_runtimes.items():
            strategies[group].runtime = runtime
        if not sample_start <= replay_event.trading_date <= sample_end:
            if replay_event.trading_date < sample_start:
                for group, output in bar_outputs.items():
                    entry = output.get("entry_evaluation")
                    if entry and entry["accepted"]:
                        warmup_evidence[group].add(entry["evidence_id"])
            continue
        selected_bars.append(m15_closed)
        selected_dates.append(replay_event.trading_date)
        for group in groups:
            group_rows[group].append(bar_outputs[group])
    return {
        "csymbol": csymbol,
        "groups": group_rows,
        "bars": selected_bars,
        "trading_dates": selected_dates,
        "warmup_evidence": warmup_evidence,
    }


def _baseline_core(row: Mapping[str, object]) -> dict[str, object]:
    excluded = {"high", "low", "experiment_group", "entry_evaluation"}
    return {key: value for key, value in row.items() if key not in excluded}


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _validate_s0_baseline(
    product_results: Mapping[str, Mapping[str, object]],
    baseline_path: Path,
) -> dict[str, object]:
    expected = {
        (str(row["csymbol"]), _eob_key(row["eob"])): row
        for row in _load_jsonl(baseline_path)
    }
    actual: dict[tuple[str, str], dict[str, object]] = {}
    for csymbol, result in product_results.items():
        rows = result["groups"]["S0"]  # type: ignore[index]
        for row in rows:
            actual[(csymbol, _eob_key(row["eob"]))] = _baseline_core(row)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatched = [key for key in set(expected) & set(actual) if expected[key] != actual[key]]
    return {
        "expected_rows": len(expected),
        "actual_rows": len(actual),
        "missing_rows": len(missing),
        "extra_rows": len(extra),
        "mismatched_rows": len(mismatched),
        "first_missing": missing[:3],
        "first_extra": extra[:3],
        "first_mismatch": sorted(mismatched)[:3],
        "passed": not missing and not extra and not mismatched,
    }


def _state_distribution(predicted: Sequence[str]) -> dict[str, object]:
    counts = Counter(predicted)
    transitions = Counter()
    for prior, current in zip(predicted, predicted[1:]):
        if prior == current:
            continue
        if prior == "none":
            transitions["none_to_direction"] += 1
        elif current == "none":
            transitions["direction_to_none"] += 1
        else:
            transitions["direction_flip"] += 1
    total = len(predicted)
    return {
        "bars": total,
        "bull_bars": counts["bull"],
        "bear_bars": counts["bear"],
        "none_bars": counts["none"],
        "direction_hold_rate": (counts["bull"] + counts["bear"]) / total if total else 0.0,
        "none_rate": counts["none"] / total if total else 0.0,
        **dict(sorted(transitions.items())),
    }


def _entry_summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    reasons = Counter()
    paths = Counter()
    accepted = 0
    for row in rows:
        entry = row.get("entry_evaluation")
        if not isinstance(entry, Mapping):
            continue
        reasons[str(entry.get("reason", "unknown"))] += 1
        if bool(entry.get("accepted", False)):
            accepted += 1
            paths[str(entry.get("path", "unknown"))] += 1
    return {
        "context": "FACTS_ONLY_NO_POSITION_OR_FILL_CONTEXT",
        "evaluated_bars": sum(reasons.values()),
        "accepted_path_candidates": accepted,
        "accepted_by_path": dict(sorted(paths.items())),
        "blocked_or_reason_counts": dict(sorted(reasons.items())),
        "real_order_count_inference_allowed": False,
    }


def _decision_summary(rows: Sequence[Mapping[str, object]], group: str) -> dict[str, object]:
    actions = Counter()
    reasons = Counter()
    confirmed = 0
    candidate_votes = 0
    candidate_confirmed = 0
    candidate_resets = 0
    prefix = f"{group}_" if group in {"A", "B"} else ""
    for index, row in enumerate(rows):
        decision = row["trend_decision"]
        action = str(decision["proposed_action"])
        reason = str(decision["reason_code"])
        actions[action] += 1
        reasons[reason] += 1
        confirmed += int(bool(decision["confirmed"]))
        if prefix and reason.startswith(prefix) and action != "veto":
            candidate_votes += 1
            candidate_confirmed += int(bool(decision["confirmed"]))
            if not decision["confirmed"] and index + 1 < len(rows):
                next_decision = rows[index + 1]["trend_decision"]
                next_state = next_decision.get("state", {})
                if (
                    next_decision.get("proposed_action") in {"maintain", "veto"}
                    and isinstance(next_state, Mapping)
                    and int(next_state.get("leave_votes", 0)) == 0
                ):
                    candidate_resets += 1
    return {
        "actions": dict(sorted(actions.items())),
        "reasons": dict(sorted(reasons.items())),
        "confirmed_state_changes": confirmed,
        "candidate_vote_rows": candidate_votes,
        "candidate_confirmed_exits": candidate_confirmed,
        "candidate_first_votes_cleared_by_maintain_or_veto": candidate_resets,
    }


def _score_product(
    csymbol: str,
    rows: Sequence[Mapping[str, object]],
    bars: Sequence[Bar],
    trading_dates: Sequence[date],
    frozen_events: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if len(rows) != len(bars) or len(rows) != len(trading_dates):
        raise ValueError(f"score inputs are misaligned for {csymbol}")
    atr = [float(row["atr14"]) for row in rows]
    predicted = [_predicted_state(row) for row in rows]
    reference = future_48bar_3atr_first_touch(bars, atr)
    bar_metrics = score_legacy_first_touch_metrics(
        bars,
        atr,
        predicted,
        reference,
        trading_dates,
        symbols=[csymbol] * len(rows),
    )
    generated = atr_directional_change_reference(bars, atr)
    generated_ids = [event.event_id for event in generated.events]
    frozen_ids = [str(event["event_id"]) for event in frozen_events]
    if generated_ids != frozen_ids:
        raise ValueError(f"frozen reference event identity mismatch for {csymbol}")
    independent = score_confirmed_turn_events(predicted, generated.events)
    for detail in independent["events"]:
        detail["csymbol"] = csymbol
    leg = score_reference_leg_events(
        predicted,
        [{**event.to_dict(), "csymbol": csymbol} for event in generated.events],
    )
    return {
        "bar_metrics": {key: bar_metrics[key] for key in BAR_KEYS},
        "independent_events": independent,
        "leg_events": leg,
        "state_distribution": _state_distribution(predicted),
        "entry": _entry_summary(rows),
        "decisions": _decision_summary(rows, str(rows[0].get("experiment_group", ""))),
        "predicted": predicted,
        "reference": reference,
        "atr": atr,
    }


def _pool_group_score(
    product_scores: Mapping[str, Mapping[str, object]],
    product_results: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    bars: list[Bar] = []
    atr: list[float] = []
    predicted: list[str] = []
    reference: list[int | None] = []
    dates: list[date] = []
    symbols: list[str] = []
    independent_details: list[dict[str, object]] = []
    leg_details: list[dict[str, object]] = []
    for csymbol, score in sorted(product_scores.items()):
        local_bars = product_results[csymbol]["bars"]
        local_dates = product_results[csymbol]["trading_dates"]
        bars.extend(local_bars)
        dates.extend(local_dates)
        atr.extend(score["atr"])
        predicted.extend(score["predicted"])
        reference.extend(score["reference"])
        symbols.extend([csymbol] * len(local_bars))
        independent_details.extend(score["independent_events"]["events"])
        leg_details.extend(score["leg_events"]["events"])
    bar_metrics = score_legacy_first_touch_metrics(
        bars, atr, predicted, reference, dates, symbols=symbols
    )
    equal_weight = {
        key: mean(float(score["bar_metrics"][key]) for score in product_scores.values())
        for key in ("direction_agreement", "trend_segment_coverage", "range_switches_per_product_day")
    }
    return {
        "bar_weighted": {key: bar_metrics[key] for key in BAR_KEYS},
        "product_equal_weighted": equal_weight,
        "independent_events": summarize_scored_event_details(independent_details),
        "leg_events": summarize_leg_event_details(leg_details),
        "state_distribution": {
            key: sum(int(score["state_distribution"].get(key, 0)) for score in product_scores.values())
            for key in (
                "bars",
                "bull_bars",
                "bear_bars",
                "none_bars",
                "none_to_direction",
                "direction_to_none",
                "direction_flip",
            )
        },
        "entry": {
            "context": "FACTS_ONLY_NO_POSITION_OR_FILL_CONTEXT",
            "accepted_path_candidates": sum(
                int(score["entry"]["accepted_path_candidates"]) for score in product_scores.values()
            ),
            "real_order_count_inference_allowed": False,
        },
    }


def _event_exposure(predicted: Sequence[str], event: Mapping[str, object]) -> dict[str, object]:
    start = int(event["confirmation_index"])
    end = int(event["leg_valid_end_index"])
    old = str(event["old_direction"])
    new = str(event["new_direction"])
    states = predicted[start : end + 1]
    first_exit = next((start + i for i, state in enumerate(states) if state != old), None)
    return {
        "leg_status": event["leg_status"],
        "within_4_bars": bool(event["within_4_bars"]),
        "leg_delay_bars": event["leg_delay_bars"],
        "first_target_index": event["first_target_index"],
        "second_confirmation_index": event["second_confirmation_index"],
        "first_exit_old_direction_index": first_exit,
        "old_direction_bars": sum(state == old for state in states),
        "new_direction_bars": sum(state == new for state in states),
        "none_bars": sum(state == "none" for state in states),
        "leg_bars": len(states),
    }


def _session(eob: str) -> str:
    bob = datetime.fromisoformat(eob) - timedelta(minutes=15)
    if bob.hour >= 20 or bob.hour < 8:
        return "night"
    return "day_am" if bob.hour < 12 else "day_pm"


def _build_event_pairs(
    group_product_scores: Mapping[str, Mapping[str, Mapping[str, object]]],
    frozen_report: Mapping[str, object],
) -> list[dict[str, object]]:
    pairs: list[dict[str, object]] = []
    for csymbol in sorted(group_product_scores["S0"]):
        group_maps = {
            group: {
                str(row["event_id"]): row
                for row in group_product_scores[group][csymbol]["leg_events"]["events"]
            }
            for group in GROUPS
        }
        frozen_events = frozen_report["per_product"][csymbol]["independent_turn_events"]["events"]
        frozen_map = {str(row["event_id"]): row for row in frozen_events}
        for event_id, s0_event in group_maps["S0"].items():
            if s0_event["leg_status"] == "censored":
                continue
            row: dict[str, object] = {
                "event_id": event_id,
                "csymbol": csymbol,
                "confirmation_eob": s0_event["confirmation_eob"],
                "confirmation_index": s0_event["confirmation_index"],
                "leg_valid_end_index": s0_event["leg_valid_end_index"],
                "old_direction": s0_event["old_direction"],
                "new_direction": s0_event["new_direction"],
                "session": _session(str(s0_event["confirmation_eob"])),
                "original_48bar_status": frozen_map[event_id]["status"],
                "original_48bar_resolved_index": frozen_map[event_id].get("resolved_index"),
                "original_late_credit": (
                    frozen_map[event_id].get("status") == "resolved"
                    and int(frozen_map[event_id]["resolved_index"]) + 1
                    >= int(s0_event.get("next_confirmation_index") or 10**18)
                ),
                "groups": {},
            }
            for group in GROUPS:
                score = group_product_scores[group][csymbol]
                row["groups"][group] = _event_exposure(  # type: ignore[index]
                    score["predicted"], group_maps[group][event_id]
                )
            pairs.append(row)
    if len(pairs) != 793:
        raise ValueError(f"event-pair denominator drifted: {len(pairs)}")
    return sorted(pairs, key=lambda row: (str(row["confirmation_eob"]), str(row["csymbol"])))


def _pair_category(s0: Mapping[str, object], candidate: Mapping[str, object]) -> str:
    s0_timely = bool(s0["within_4_bars"])
    candidate_timely = bool(candidate["within_4_bars"])
    if not s0_timely and candidate_timely:
        return "s0_slow_candidate_timely"
    if s0_timely and not candidate_timely:
        return "s0_timely_candidate_slow"
    if s0_timely and candidate_timely:
        return "both_timely"
    return "both_slow"


def _pair_summary(pairs: Sequence[Mapping[str, object]], candidate: str) -> dict[str, object]:
    categories = Counter()
    by_dimension: dict[str, dict[str, Counter]] = {
        "product": defaultdict(Counter),
        "direction": defaultdict(Counter),
        "session": defaultdict(Counter),
    }
    old_delta = new_delta = none_delta = 0
    for row in pairs:
        s0 = row["groups"]["S0"]
        other = row["groups"][candidate]
        category = _pair_category(s0, other)
        categories[category] += 1
        by_dimension["product"][str(row["csymbol"])][category] += 1
        by_dimension["direction"][str(row["new_direction"])][category] += 1
        by_dimension["session"][str(row["session"])][category] += 1
        old_delta += int(other["old_direction_bars"]) - int(s0["old_direction_bars"])
        new_delta += int(other["new_direction_bars"]) - int(s0["new_direction_bars"])
        none_delta += int(other["none_bars"]) - int(s0["none_bars"])
    return {
        "denominator": len(pairs),
        "categories": dict(sorted(categories.items())),
        "category_sum": sum(categories.values()),
        "old_direction_exposure_delta_bars": old_delta,
        "new_direction_exposure_delta_bars": new_delta,
        "none_exposure_delta_bars": none_delta,
        "by_product": {key: dict(value) for key, value in sorted(by_dimension["product"].items())},
        "by_direction": {key: dict(value) for key, value in sorted(by_dimension["direction"].items())},
        "by_session": {key: dict(value) for key, value in sorted(by_dimension["session"].items())},
    }


def _added_exits(
    group: str,
    product_results: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    prefix = f"{group}_"
    for csymbol, result in sorted(product_results.items()):
        rows = result["groups"][group]
        baseline = result["groups"]["S0"]
        bars: Sequence[Bar] = result["bars"]
        for index, row in enumerate(rows):
            decision = row["trend_decision"]
            reason = str(decision["reason_code"])
            if not (reason.startswith(prefix) and bool(decision["confirmed"])):
                continue
            prior = str(decision["prior_state"])
            after = str(decision["confirmed_state"])
            if prior not in {"bull", "bear"} or prior == after:
                continue
            atr = float(row["atr14"])
            future = list(bars[index + 1 : index + 9])
            if future:
                if prior == "bull":
                    favorable = max(bar.high for bar in future) - float(row["close"])
                    adverse = float(row["close"]) - min(bar.low for bar in future)
                else:
                    favorable = float(row["close"]) - min(bar.low for bar in future)
                    adverse = max(bar.high for bar in future) - float(row["close"])
            else:
                favorable = adverse = 0.0
            opposite = "bear" if prior == "bull" else "bull"
            future_states = [_predicted_state(item) for item in rows[index + 1 : index + 9]]
            opposite_two = any(
                future_states[item] == opposite and future_states[item + 1] == opposite
                for item in range(max(0, len(future_states) - 1))
            )
            returns_old = prior in future_states
            baseline_decision = baseline[index]["trend_decision"]
            output.append({
                "group": group,
                "csymbol": csymbol,
                "index": index,
                "eob": row["eob"],
                "prior_state": prior,
                "confirmed_state": after,
                "reason_code": reason,
                "h1_relative": row["trend_inputs"]["h1_relative"],
                "baseline_state_after": baseline_decision["confirmed_state"],
                "baseline_confirmed_transition": bool(baseline_decision["confirmed"]),
                "window_complete": len(future) == 8,
                "old_direction_favorable_atr": favorable / atr if atr > 0 else None,
                "old_direction_adverse_atr": adverse / atr if atr > 0 else None,
                "returns_to_old_direction_within_8": returns_old,
                "opposite_two_bar_state_within_8": opposite_two,
                "entry_evaluation": row.get("entry_evaluation"),
            })
    return output


def _added_exit_summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    complete = [row for row in rows if row["window_complete"]]
    return {
        "confirmed_added_exits": len(rows),
        "complete_8bar_windows": len(complete),
        "incomplete_8bar_windows": len(rows) - len(complete),
        "returns_to_old_direction_within_8": sum(bool(row["returns_to_old_direction_within_8"]) for row in rows),
        "opposite_two_bar_state_within_8": sum(bool(row["opposite_two_bar_state_within_8"]) for row in rows),
        "old_direction_favorable_atr_median": (
            median(float(row["old_direction_favorable_atr"]) for row in complete) if complete else None
        ),
        "old_direction_adverse_atr_median": (
            median(float(row["old_direction_adverse_atr"]) for row in complete) if complete else None
        ),
        "by_product": dict(sorted(Counter(str(row["csymbol"]) for row in rows).items())),
        "by_prior_direction": dict(sorted(Counter(str(row["prior_state"]) for row in rows).items())),
    }


def _roll_sensitivity(
    manifest: Mapping[str, object],
    product_results: Mapping[str, Mapping[str, object]],
    group_product_scores: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> dict[str, object]:
    contaminated: dict[str, set[int]] = {}
    immediate: dict[str, set[int]] = {}
    total_rolls = 0
    for csymbol, product in manifest["products"].items():
        dates: Sequence[date] = product_results[csymbol]["trading_dates"]
        contaminated[csymbol] = set()
        immediate[csymbol] = set()
        for raw in product.get("rolls", []):
            total_rolls += 1
            roll_date = date.fromisoformat(str(raw["trading_date"]))
            indices = [index for index, value in enumerate(dates) if value == roll_date]
            immediate[csymbol].update(indices)
            if indices:
                start = min(indices)
                contaminated[csymbol].update(range(start, min(len(dates), start + 1200)))
    group_output: dict[str, object] = {}
    for group in GROUPS:
        correct = directional = 0
        clean_leg_events: list[dict[str, object]] = []
        for csymbol, score in group_product_scores[group].items():
            predicted = score["predicted"]
            reference = score["reference"]
            for index, (actual, expected) in enumerate(zip(predicted, reference)):
                if index in contaminated[csymbol] or expected not in (-1, 1):
                    continue
                directional += 1
                correct += int((1 if actual == "bull" else -1 if actual == "bear" else 0) == expected)
            for event in score["leg_events"]["events"]:
                if event["leg_status"] == "censored":
                    continue
                start = int(event["confirmation_index"])
                end = int(event["leg_valid_end_index"])
                if any(index in contaminated[csymbol] for index in range(start, end + 1)):
                    continue
                clean_leg_events.append(dict(event))
        leg = summarize_leg_event_details(clean_leg_events)
        group_output[group] = {
            "directional_reference_bars": directional,
            "direction_agreement": correct / directional if directional else None,
            "clean_leg_events": leg["scoreable_events"],
            "within_4_bars": leg["within_4_bars"],
            "penalized_delay_median": leg["penalized_delay_median"],
            "trend_segment_coverage": None,
            "trend_segment_coverage_reason": "NOT_COMPUTED_ACROSS_REMOVED_WARMUP_PROPAGATION_GAPS",
        }
    return {
        "rolls": total_rolls,
        "lookahead_risk": "same-roll-date closing difference is applied to earlier bars on the roll date",
        "immediate_roll_day_rows": sum(len(value) for value in immediate.values()),
        "conservative_roll_day_plus_1200_m15_rows": sum(len(value) for value in contaminated.values()),
        "total_rows": sum(len(result["bars"]) for result in product_results.values()),
        "groups": group_output,
        "hard_gate_decision_allowed_from_sensitivity": False,
    }


def _timeline_point(row: Mapping[str, object]) -> dict[str, object]:
    decision = row["trend_decision"]
    inputs = row["trend_inputs"]
    state = decision["state"]
    return {
        "eob": row["eob"],
        "prior_state": decision["prior_state"],
        "state_after": decision["confirmed_state"],
        "close_side": inputs["close_side"],
        "s1_extreme_then_flat": inputs["s1_extreme_then_flat"],
        "s2_bearish_pattern": inputs["s2_bearish_pattern"],
        "s2_bullish_pattern": inputs["s2_bullish_pattern"],
        "p_close_cross": inputs["p_close_cross"],
        "p_macd_cross": inputs["p_macd_cross"],
        "h1_relative": inputs["h1_relative"],
        "proposed_action": decision["proposed_action"],
        "reason_code": decision["reason_code"],
        "leave_votes": state["leave_votes"],
        "confirmed": decision["confirmed"],
    }


def _case_studies(
    pairs: Sequence[Mapping[str, object]],
    added: Mapping[str, Sequence[Mapping[str, object]]],
    product_results: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    for candidate in ("A", "B"):
        rescue = [
            row for row in pairs
            if _pair_category(row["groups"]["S0"], row["groups"][candidate])
            == "s0_slow_candidate_timely"
        ][:2]
        harm = [
            row for row in pairs
            if _pair_category(row["groups"]["S0"], row["groups"][candidate])
            == "s0_timely_candidate_slow"
        ][:2]
        for kind, rows in (("candidate_rescue", rescue), ("candidate_harm", harm)):
            for row in rows:
                candidates.append({"kind": kind, "candidate": candidate, "event": row})
    for row in [item for item in added["B"] if not item["opposite_two_bar_state_within_8"]][:2]:
        candidates.append({"kind": "b_exit_without_new_direction", "candidate": "B", "exit": row})
    false_breakouts = sorted(
        [
            item for group in ("A", "B") for item in added[group]
            if item["old_direction_favorable_atr"] is not None
            and float(item["old_direction_favorable_atr"]) >= 1.0
        ],
        key=lambda row: (str(row["eob"]), str(row["csymbol"])),
    )[:2]
    for row in false_breakouts:
        candidates.append({"kind": "old_trend_extended_at_least_1atr", "candidate": row["group"], "exit": row})
    selected = candidates[:8]
    for case in selected:
        candidate = str(case["candidate"])
        source = case.get("event", case.get("exit"))
        csymbol = str(source["csymbol"])
        rows = product_results[csymbol]["groups"][candidate]
        baseline = product_results[csymbol]["groups"]["S0"]
        center = int(source.get("confirmation_index", source.get("index")))
        end = min(len(rows) - 1, center + 8)
        start = max(0, center - 2)
        first_divergence = next(
            (
                index for index in range(len(rows))
                if _predicted_state(rows[index]) != _predicted_state(baseline[index])
            ),
            None,
        )
        case["first_group_divergence_index"] = first_divergence
        case["timeline"] = [
            {
                "index": index,
                "S0": _timeline_point(baseline[index]),
                candidate: _timeline_point(rows[index]),
            }
            for index in range(start, end + 1)
        ]
    late = next((row for row in pairs if row["original_late_credit"]), None)
    return {
        "selection_rule": "earliest two per fixed category; old-trend extension means >=1 ATR in next 8 M15 bars",
        "selected_count": len(selected),
        "cases": selected,
        "late_credit_public_example": late,
    }


def _decision(
    group_reports: Mapping[str, Mapping[str, object]],
    pair_summaries: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    qualified: list[str] = []
    s0_timely = int(group_reports["S0"]["pool"]["leg_events"]["within_4_bars"])
    for candidate in ("A", "B"):
        pool = group_reports[candidate]["pool"]
        hard = pool["hard_gates"]
        improves_timely = int(pool["leg_events"]["within_4_bars"]) > s0_timely
        reduces_wrong = int(pair_summaries[candidate]["old_direction_exposure_delta_bars"]) < 0
        if all(hard.values()) and improves_timely and reduces_wrong:
            qualified.append(candidate)
    if not qualified:
        return {
            "decision": "STOP_EXIT_RULE_MODIFICATION_ROUTE",
            "frozen_candidate": None,
            "reason": "No candidate passed all four hard gates plus in-leg timely and wrong-exposure requirements; no third variant is allowed on this window.",
        }
    ranked = sorted(
        qualified,
        key=lambda group: (
            -int(group_reports[group]["pool"]["leg_events"]["within_4_bars"]),
            float(group_reports[group]["pool"]["bar_weighted"]["range_switches_per_product_day"]),
            0 if group == "B" else 1,
        ),
    )
    return {
        "decision": "FREEZE_ONE_RESEARCH_CANDIDATE_FOR_READ_ONLY_FORWARD_OBSERVATION",
        "frozen_candidate": ranked[0],
        "reason": "Candidate met all frozen gates and tie-break rules.",
    }


def run_experiment(
    *,
    data_root: Path,
    reference_report_path: Path,
    code_sha: str,
    workers: int = 1,
) -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]]:
    manifest = validate_dataset_manifest(data_root)
    if manifest.get("purpose") != "SPRINT2_ROLLING_180D":
        raise ValueError("dataset purpose mismatch")
    if manifest.get("start_date") != SAMPLE_START.isoformat() or manifest.get("end_date") != SAMPLE_END.isoformat():
        raise ValueError("dataset range mismatch")
    config = load_config(default_config_path())
    if config.execute_enabled:
        raise ValueError("execute_enabled must remain false")
    reference_report = json.loads(reference_report_path.read_text(encoding="utf-8"))
    boundary = public_boundary_counts(reference_report)
    if boundary != EXPECTED_BOUNDARY_COUNTS:
        raise ValueError(f"public boundary counts mismatch: {boundary}")
    products = manifest["products"]
    tasks = [
        (
            str(data_root),
            str(product["continuous_path"]),
            str(csymbol),
            dict(config.raw["rules"]),
            config.subscription_count,
            GROUPS,
        )
        for csymbol, product in sorted(products.items())
    ]
    raw_results: list[dict[str, object]] = []
    if workers <= 1:
        for task in tasks:
            raw_results.append(_run_product(*task))
    else:
        with ProcessPoolExecutor(max_workers=min(workers, len(tasks))) as executor:
            futures = {executor.submit(_run_product, *task): task[2] for task in tasks}
            for future in as_completed(futures):
                result = future.result()
                raw_results.append(result)
                print(json.dumps({
                    "event": "EXIT_EXPERIMENT_PRODUCT_DONE",
                    "csymbol": result["csymbol"],
                    "facts": len(result["bars"]),
                }, ensure_ascii=False), flush=True)
    product_results = {str(result["csymbol"]): result for result in raw_results}
    baseline_validation = _validate_s0_baseline(
        product_results,
        data_root / "baseline_facts.jsonl",
    )
    if not baseline_validation["passed"]:
        raise ValueError(f"S0 baseline reproduction failed: {baseline_validation}")

    group_product_scores: dict[str, dict[str, dict[str, object]]] = {
        group: {} for group in GROUPS
    }
    for group in GROUPS:
        for csymbol, result in sorted(product_results.items()):
            frozen_events = reference_report["per_product"][csymbol]["independent_turn_events"]["events"]
            group_product_scores[group][csymbol] = _score_product(
                csymbol,
                result["groups"][group],
                result["bars"],
                result["trading_dates"],
                frozen_events,
            )
    group_reports: dict[str, object] = {}
    for group in GROUPS:
        pool = _pool_group_score(group_product_scores[group], product_results)
        bars = pool["bar_weighted"]
        independent = pool["independent_events"]
        pool["hard_gates"] = {
            "direction_agreement": float(bars["direction_agreement"]) >= 0.60,
            "trend_segment_coverage": float(bars["trend_segment_coverage"]) >= 0.70,
            "range_switches": float(bars["range_switches_per_product_day"]) <= 1.0,
            "turn_delay": float(independent["turn_delay_median"]) <= 4,
        }
        group_reports[group] = {
            "pool": pool,
            "per_product": {
                csymbol: {
                    key: (
                        {item: metric for item, metric in value.items() if item != "events"}
                        if key in {"independent_events", "leg_events"}
                        else value
                    )
                    for key, value in score.items()
                    if key not in {"predicted", "reference", "atr"}
                }
                for csymbol, score in group_product_scores[group].items()
            },
        }
    pairs = _build_event_pairs(group_product_scores, reference_report)
    pair_summaries = {group: _pair_summary(pairs, group) for group in ("EMA34_CONTROL", "A", "B")}
    added = {group: _added_exits(group, product_results) for group in ("A", "B")}
    added_summary = {group: _added_exit_summary(rows) for group, rows in added.items()}
    roll = _roll_sensitivity(manifest, product_results, group_product_scores)
    cases = _case_studies(pairs, added, product_results)
    decision = _decision(group_reports, pair_summaries)
    report = {
        "schema_version": 1,
        "purpose": "SPRINT2_EXIT_RULE_FOUR_GROUP_EXPERIMENT",
        "report_status": "ROLLING_180D_RESEARCH",
        "performance_interpretation_allowed": False,
        "simulation_or_live_orders_used": False,
        "base_sha": BASE_SHA,
        "code_sha": code_sha,
        "dataset": {
            "id": "gm-rolling-180d-163ec775a06853f54078",
            "manifest_sha256": _sha256(data_root / "manifest.json"),
            "baseline_facts_sha256": _sha256(data_root / "baseline_facts.jsonl"),
            "strategy_config_sha256": _sha256(default_config_path()),
            "products": len(products),
            "complete_m15_facts": sum(len(result["bars"]) for result in product_results.values()),
            "independent_validation": False,
        },
        "public_boundary_reproduction": boundary,
        "s0_baseline_reproduction": baseline_validation,
        "groups": group_reports,
        "paired_event_summaries": pair_summaries,
        "added_exit_summaries": added_summary,
        "roll_lookahead_sensitivity": roll,
        "case_studies": cases,
        "decision": decision,
        "limitations": [
            "The 180-day window has already been used for research and is not independent validation.",
            "Facts-only entry candidates have no positions, fills, fees, portfolio limits, or broker context.",
            "Same-roll-date closing differences create lookahead risk for earlier roll-day bars.",
            "Unverified fees prohibit net performance evaluation.",
        ],
    }
    added_output = {
        "schema_version": 1,
        "purpose": "SPRINT2_EXIT_RULE_ADDED_EXIT_PRICE_PATHS",
        "performance_interpretation_allowed": False,
        "groups": added,
        "summaries": added_summary,
    }
    return report, pairs, added_output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the frozen four-group exit-rule experiment")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument(
        "--groups",
        default=",".join(GROUPS),
        help="Frozen interface; must be S0,EMA34_CONTROL,A,B",
    )
    parser.add_argument("--reference-report", type=Path, default=Path("reports/sprint2_p0b_reference_comparison.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    requested = tuple(item.strip() for item in args.groups.split(",") if item.strip())
    if requested != GROUPS:
        parser.error(f"groups are frozen and must be {','.join(GROUPS)}")
    report, pairs, added = run_experiment(
        data_root=args.data_root,
        reference_report_path=args.reference_report,
        code_sha=args.code_sha,
        workers=max(1, args.workers),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "summary": (args.output_dir / "summary.json", report),
        "event_pairs": (args.output_dir / "event_pairs.json", {
            "schema_version": 1,
            "purpose": "SPRINT2_EXIT_RULE_EVENT_PAIRS",
            "scoreable_events": len(pairs),
            "censored_events": 17,
            "events": pairs,
        }),
        "added_exits": (args.output_dir / "added_exits.json", added),
    }
    for path, value in outputs.values():
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({
        "outputs": {key: str(value[0]) for key, value in outputs.items()},
        "decision": report["decision"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
