from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from statistics import median
from typing import Iterable, Mapping, Sequence

import pandas as pd

from futures_trend_quant.data.development_export import assign_trading_date, validate_dataset_manifest

from .trend_report import _eob_key, _load_facts, _predicted_state


COMPONENTS = ("divergence", "s1", "s2", "p", "r", "path1", "path2")


def _decision(row: Mapping[str, object]) -> Mapping[str, object]:
    value = row.get("trend_decision")
    return value if isinstance(value, Mapping) else {}


def _state(row: Mapping[str, object]) -> str:
    try:
        return _predicted_state(row)
    except ValueError:
        return "unknown"


def component_snapshot(row: Mapping[str, object], old_direction: str) -> dict[str, object]:
    """Normalize leave evidence using the state that produced this bar's facts."""

    decision = _decision(row)
    input_state = str(decision.get("prior_state", _state(row)))
    output_state = _state(row)
    if input_state != old_direction:
        return {
            "eligible": False,
            "strategy_state": input_state,
            "state_after": output_state,
        }
    inputs = row.get("trend_inputs")
    if not isinstance(inputs, Mapping):
        return {
            "eligible": False,
            "strategy_state": input_state,
            "state_after": output_state,
            "missing_inputs": True,
        }
    if old_direction == "bull":
        divergence = bool(row.get("top_div", False))
        s2 = bool(inputs.get("s2_bearish_pattern", False))
        reverse = bool(inputs.get("r_bearish_reverse", False))
    else:
        divergence = bool(row.get("bottom_div", False))
        s2 = bool(inputs.get("s2_bullish_pattern", False))
        reverse = bool(inputs.get("r_bullish_reverse", False))
    s1 = bool(inputs.get("s1_extreme_then_flat", False))
    p_close = bool(inputs.get("p_close_cross", False))
    p_macd = bool(inputs.get("p_macd_cross", False))
    p_any = p_close or p_macd
    return {
        "eligible": True,
        "strategy_state": old_direction,
        "state_after": output_state,
        "divergence": divergence,
        "s1": s1,
        "s2": s2,
        "p": p_any,
        "p_close": p_close,
        "p_macd": p_macd,
        "r": reverse,
        "path1": divergence and (s1 or s2) and p_any,
        "path2": reverse,
        "h1_relative": str(inputs.get("h1_relative", "unknown")),
        "h1_absolute": str(row.get("h1_absolute", "unknown")),
    }


def _occurrence(row: Mapping[str, object], index: int, confirmation_index: int) -> dict[str, object]:
    return {
        "index": index,
        "delay_bars": index - confirmation_index,
        "eob": str(row.get("eob", "")),
        "bar_id": str(row.get("bar_id", "")),
    }


def _first_matching(
    rows: Sequence[Mapping[str, object]],
    start: int,
    end: int,
    confirmation_index: int,
    predicate,
) -> dict[str, object] | None:
    for index in range(start, end + 1):
        if predicate(rows[index]):
            return _occurrence(rows[index], index, confirmation_index)
    return None


def _session(confirmation_eob: str) -> str:
    bob = datetime.fromisoformat(confirmation_eob) - timedelta(minutes=15)
    if bob.hour >= 20 or bob.hour < 8:
        return "night"
    if bob.hour < 12:
        return "day_am"
    return "day_pm"


def diagnose_event(
    rows: Sequence[Mapping[str, object]],
    event: Mapping[str, object],
    *,
    actual_contract: str = "unknown",
    trading_date: str = "unknown",
    roll_bucket: str = "unknown",
) -> dict[str, object]:
    confirmation_index = int(event["confirmation_index"])
    end_index = int(event["observation_end_index"])
    if not 0 <= confirmation_index < len(rows) or not confirmation_index <= end_index < len(rows):
        raise ValueError("event indices are outside fact rows")
    if _eob_key(rows[confirmation_index].get("eob")) != _eob_key(event["confirmation_eob"]):
        raise ValueError("event confirmation does not match fact row")
    old_direction = str(event["old_direction"])
    new_direction = str(event["new_direction"])
    start_index = max(0, confirmation_index - 48)
    at_event = component_snapshot(rows[confirmation_index], old_direction)

    first_components: dict[str, object] = {}
    for component in COMPONENTS:
        first_components[component] = _first_matching(
            rows,
            start_index,
            end_index,
            confirmation_index,
            lambda row, key=component: bool(component_snapshot(row, old_direction).get(key, False)),
        )

    h1_first: dict[str, object] = {}
    for h1_state in ("strong", "weakening", "flat", "reverse"):
        h1_first[h1_state] = _first_matching(
            rows,
            start_index,
            end_index,
            confirmation_index,
            lambda row, expected=h1_state: component_snapshot(row, old_direction).get("h1_relative") == expected,
        )

    first_transition_proposal = _first_matching(
        rows,
        confirmation_index,
        end_index,
        confirmation_index,
        lambda row: str(_decision(row).get("proposed_action", ""))
        in {"veto", "end_to_none", "flip", "strong_exit"},
    )
    first_leave_vote = _first_matching(
        rows,
        confirmation_index,
        end_index,
        confirmation_index,
        lambda row: str(_decision(row).get("reason_code", "")) == "LEAVE_VOTE",
    )
    first_new_confirmation = _first_matching(
        rows,
        confirmation_index,
        end_index,
        confirmation_index,
        lambda row: bool(_decision(row).get("confirmed", False))
        and str(_decision(row).get("confirmed_state", "")) == new_direction,
    )

    action_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    joint_counts: Counter[str] = Counter()
    veto_blocked_end_rows = 0
    veto_blocked_reverse_rows = 0
    for index in range(confirmation_index, end_index + 1):
        row = rows[index]
        decision = _decision(row)
        action = str(decision.get("proposed_action", "unknown"))
        action_counts[action] += 1
        reason_counts[str(decision.get("reason_code", "unknown"))] += 1
        snapshot = component_snapshot(row, old_direction)
        if not snapshot.get("eligible"):
            continue
        joint = "".join("1" if snapshot.get(key) else "0" for key in COMPONENTS)
        joint_counts[joint] += 1
        if action == "veto" and snapshot.get("path1"):
            veto_blocked_end_rows += 1
        if action == "veto" and snapshot.get("path2"):
            veto_blocked_reverse_rows += 1

    missing_path1 = []
    if not at_event.get("eligible"):
        missing_path1.append("strategy_state_not_old_direction")
    else:
        if not at_event.get("divergence"):
            missing_path1.append("divergence")
        if not (at_event.get("s1") or at_event.get("s2")):
            missing_path1.append("s1_or_s2")
        if not at_event.get("p"):
            missing_path1.append("p")

    before_state = _state(rows[confirmation_index - 1]) if confirmation_index > 0 else None
    after_state = _state(rows[confirmation_index + 1]) if confirmation_index + 1 < len(rows) else None
    end_state = _state(rows[end_index])
    resolved_index = event.get("resolved_index")
    significant_indices = {
        confirmation_index,
        end_index,
        *(
            int(item["index"])
            for item in first_components.values()
            if isinstance(item, Mapping)
        ),
    }
    if isinstance(resolved_index, int):
        significant_indices.add(resolved_index)
    timeline = []
    for index in sorted(significant_indices):
        row = rows[index]
        decision = _decision(row)
        timeline.append({
            **_occurrence(row, index, confirmation_index),
            "strategy_state": _state(row),
            "proposed_action": str(decision.get("proposed_action", "unknown")),
            "reason_code": str(decision.get("reason_code", "unknown")),
            "confirmed": bool(decision.get("confirmed", False)),
            "confirmed_state": str(decision.get("confirmed_state", "unknown")),
            "leave_votes": int(
                decision.get("state", {}).get("leave_votes", 0)
                if isinstance(decision.get("state"), Mapping)
                else 0
            ),
            "components": component_snapshot(row, old_direction),
        })

    return {
        "event_id": str(event["event_id"]),
        "source": str(event["source"]),
        "csymbol": str(event.get("csymbol", event.get("symbol", ""))),
        "actual_contract": actual_contract,
        "trading_date": trading_date,
        "roll_bucket": roll_bucket,
        "session": _session(str(event["confirmation_eob"])),
        "old_direction": old_direction,
        "new_direction": new_direction,
        "pivot_eob": str(event["pivot_eob"]),
        "confirmation_eob": str(event["confirmation_eob"]),
        "confirmation_bar_id": str(rows[confirmation_index].get("bar_id", "")),
        "status": str(event["status"]),
        "delay_bars": event.get("delay_bars"),
        "observation_complete": bool(event["observation_complete"]),
        "observation_end_eob": str(rows[end_index].get("eob", "")),
        "strategy_state_before": before_state,
        "strategy_state_at_event": _state(rows[confirmation_index]),
        "strategy_state_after": after_state,
        "strategy_state_at_window_end": end_state,
        "components_at_event": at_event,
        "missing_path1_conditions_at_event": missing_path1,
        "path2_missing_at_event": not bool(at_event.get("path2", False)),
        "first_component_occurrences": first_components,
        "first_h1_relative_occurrences": h1_first,
        "first_transition_proposal": first_transition_proposal,
        "first_leave_vote": first_leave_vote,
        "first_new_direction_confirmation": first_new_confirmation,
        "first_two_bar_resolution_index": resolved_index,
        "action_counts_after_event": dict(sorted(action_counts.items())),
        "reason_counts_after_event": dict(sorted(reason_counts.items())),
        "component_joint_counts_after_event": dict(sorted(joint_counts.items())),
        "h1_shadow": {
            "veto_blocked_end_evidence_rows": veto_blocked_end_rows,
            "veto_blocked_reverse_evidence_rows": veto_blocked_reverse_rows,
        },
        "timeline": timeline,
    }


def _delay_summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    delays = [
        int(row["delay_bars"])
        for row in rows
        if row.get("status") in {"resolved", "unresolved"}
    ]
    return {
        "events": len(rows),
        "scoreable": len(delays),
        "censored": sum(row.get("status") == "censored" for row in rows),
        "unresolved": sum(row.get("status") == "unresolved" for row in rows),
        "slow_over_4": sum(delay > 4 for delay in delays),
        "delay_median": median(delays) if delays else 0.0,
    }


def summarize_diagnostics(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    component_at_event = Counter()
    component_missing = Counter()
    first_delays: dict[str, list[int]] = defaultdict(list)
    action_at_event = Counter()
    reason_at_event = Counter()
    joint_at_event = Counter()
    shadow_end_events = 0
    shadow_reverse_events = 0
    for row in rows:
        components = row["components_at_event"]
        if isinstance(components, Mapping) and components.get("eligible"):
            joint_at_event["".join("1" if components.get(key) else "0" for key in COMPONENTS)] += 1
            for component in COMPONENTS:
                component_at_event[component] += int(bool(components.get(component, False)))
        for missing in row["missing_path1_conditions_at_event"]:
            component_missing[str(missing)] += 1
        occurrences = row["first_component_occurrences"]
        if isinstance(occurrences, Mapping):
            for component in COMPONENTS:
                item = occurrences.get(component)
                if isinstance(item, Mapping):
                    first_delays[component].append(int(item["delay_bars"]))
                else:
                    component_missing[f"{component}_never_in_window"] += 1
        timeline = row["timeline"]
        event_point = next(
            (item for item in timeline if int(item["delay_bars"]) == 0),
            None,
        )
        if isinstance(event_point, Mapping):
            action_at_event[str(event_point["proposed_action"])] += 1
            reason_at_event[str(event_point["reason_code"])] += 1
        shadow = row["h1_shadow"]
        if isinstance(shadow, Mapping):
            shadow_end_events += int(int(shadow["veto_blocked_end_evidence_rows"]) > 0)
            shadow_reverse_events += int(int(shadow["veto_blocked_reverse_evidence_rows"]) > 0)

    first_occurrence_summary = {}
    for component in COMPONENTS:
        values = sorted(first_delays.get(component, []))
        first_occurrence_summary[component] = {
            "observed_events": len(values),
            "missing_events": len(rows) - len(values),
            "median_delay_bars": median(values) if values else None,
            "min_delay_bars": min(values) if values else None,
            "max_delay_bars": max(values) if values else None,
        }

    def groups(key: str) -> dict[str, object]:
        grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
        for row in rows:
            grouped[str(row[key])].append(row)
        return {name: _delay_summary(items) for name, items in sorted(grouped.items())}

    return {
        **_delay_summary(rows),
        "component_true_at_event": dict(sorted(component_at_event.items())),
        "component_missing_counts": dict(sorted(component_missing.items())),
        "component_joint_counts_at_event": dict(sorted(joint_at_event.items())),
        "first_component_delay": first_occurrence_summary,
        "action_at_event": dict(sorted(action_at_event.items())),
        "reason_at_event": dict(sorted(reason_at_event.items())),
        "h1_shadow_events": {
            "veto_blocked_end_evidence": shadow_end_events,
            "veto_blocked_reverse_evidence": shadow_reverse_events,
        },
        "by_product": groups("csymbol"),
        "by_session": groups("session"),
        "by_direction": groups("new_direction"),
        "by_roll_bucket": groups("roll_bucket"),
    }


def _load_market_context(root: Path) -> dict[str, dict[str, object]]:
    manifest = validate_dataset_manifest(root)
    output: dict[str, dict[str, object]] = {}
    products = manifest["products"]
    if not isinstance(products, Mapping):
        raise ValueError("manifest products must be a mapping")
    for csymbol, product in products.items():
        if not isinstance(product, Mapping):
            raise ValueError(f"invalid manifest product {csymbol}")
        mapping_frame = pd.read_parquet(root / str(product["mapping_path"]))
        date_column = "trading_date" if "trading_date" in mapping_frame.columns else "trade_date"
        if date_column not in mapping_frame.columns or "symbol" not in mapping_frame.columns:
            raise ValueError(f"mapping for {csymbol} lacks trading date or symbol")
        mapping = {
            date.fromisoformat(str(row[date_column])): str(row["symbol"])
            for row in mapping_frame.to_dict("records")
        }
        trading_dates = sorted(mapping)
        rolls = [date.fromisoformat(str(item["trading_date"])) for item in product.get("rolls", [])]
        output[str(csymbol)] = {
            "mapping": mapping,
            "trading_dates": trading_dates,
            "roll_dates": rolls,
        }
    return output


def _event_market_fields(event: Mapping[str, object], context: Mapping[str, object]) -> tuple[str, str, str]:
    eob = datetime.fromisoformat(str(event["confirmation_eob"]))
    bob = eob - timedelta(minutes=15)
    trading_dates = context["trading_dates"]
    mapping = context["mapping"]
    roll_dates = context["roll_dates"]
    if not isinstance(trading_dates, Sequence) or not isinstance(mapping, Mapping) or not isinstance(roll_dates, Sequence):
        raise ValueError("invalid market context")
    event_date = assign_trading_date(bob, trading_dates)
    contract = str(mapping[event_date])
    date_index = {value: index for index, value in enumerate(trading_dates)}
    event_index = date_index[event_date]
    nearest: int | None = None
    for roll_date in roll_dates:
        if roll_date not in date_index:
            continue
        distance = event_index - date_index[roll_date]
        if nearest is None or abs(distance) < abs(nearest):
            nearest = distance
    if nearest == 0:
        bucket = "roll_day"
    elif nearest is not None and -3 <= nearest <= -1:
        bucket = "pre_roll_1_3"
    elif nearest is not None and 1 <= nearest <= 3:
        bucket = "post_roll_1_3"
    else:
        bucket = "non_roll"
    return contract, event_date.isoformat(), bucket


def build_event_diagnostics(
    facts: Iterable[Mapping[str, object]],
    reference_report: Mapping[str, object],
    *,
    data_root: Path,
    code_sha: str,
) -> dict[str, object]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for raw in facts:
        row = dict(raw)
        grouped[str(row.get("csymbol", ""))].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: _eob_key(row.get("eob")))

    pool = reference_report.get("pool")
    if not isinstance(pool, Mapping):
        raise ValueError("reference report has no pool")
    independent = pool.get("independent_turn_events")
    if not isinstance(independent, Mapping) or not isinstance(independent.get("events"), list):
        raise ValueError("reference report has no independent event details")
    events = independent["events"]
    market_context = _load_market_context(data_root)
    diagnostics: list[dict[str, object]] = []
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("invalid event detail")
        event_id = str(event["event_id"])
        if event_id in seen:
            raise ValueError(f"duplicate event_id {event_id}")
        seen.add(event_id)
        csymbol = str(event["csymbol"])
        contract, event_date, roll_bucket = _event_market_fields(event, market_context[csymbol])
        diagnostics.append(
            diagnose_event(
                grouped[csymbol],
                event,
                actual_contract=contract,
                trading_date=event_date,
                roll_bucket=roll_bucket,
            )
        )
    summary = summarize_diagnostics(diagnostics)
    slow = [
        row
        for row in diagnostics
        if row.get("status") in {"resolved", "unresolved"}
        and isinstance(row.get("delay_bars"), int)
        and int(row["delay_bars"]) > 4
    ]
    slow.sort(key=lambda row: (int(row["delay_bars"]), str(row["event_id"])), reverse=True)
    return {
        "schema_version": 1,
        "purpose": "SPRINT2_P0C_EVENT_COMPONENT_DIAGNOSTICS",
        "report_status": "ROLLING_180D_RESEARCH",
        "code_sha": code_sha,
        "dataset_label": reference_report.get("dataset_label"),
        "reference_definition": "ATR_DIRECTIONAL_CHANGE_V1",
        "performance_interpretation_allowed": False,
        "strategy_behavior_changed": False,
        "summary": summary,
        "slow_summary": summarize_diagnostics(slow),
        "slow_event_sample_count": min(30, len(slow)),
        "slow_event_sample": slow[:30],
        "events": diagnostics,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose components around frozen turn events")
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--reference-report", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    reference_report = json.loads(args.reference_report.read_text(encoding="utf-8"))
    report = build_event_diagnostics(
        _load_facts(args.facts),
        reference_report,
        data_root=args.data_root,
        code_sha=args.code_sha,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output),
        "events": report["summary"]["events"],
        "slow_sample": report["slow_event_sample_count"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
