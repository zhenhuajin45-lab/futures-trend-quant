from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
from statistics import median
from typing import Iterable, Mapping, Sequence

from futures_trend_quant.core.models import Bar

from .metrics import future_48bar_3atr_zigzag
from .trend_report import _eob_key, _load_facts, _load_m15_bars, _predicted_state


def _decision_value(row: Mapping[str, object], key: str, default: str = "unknown") -> str:
    decision = row.get("trend_decision")
    if not isinstance(decision, Mapping):
        return default
    value = decision.get(key)
    return default if value is None else str(value)


def _state_value(row: Mapping[str, object]) -> str:
    try:
        return _predicted_state(row)
    except ValueError:
        return "unknown"


def _leave_inputs(row: Mapping[str, object], old_direction: str) -> dict[str, object]:
    inputs = row.get("trend_inputs")
    if not isinstance(inputs, Mapping):
        return {"available": False}
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
        "available": True,
        "divergence": divergence,
        "s1": s1,
        "s2": s2,
        "p_close": p_close,
        "p_macd": p_macd,
        "p_any": p_any,
        "r_reverse": reverse,
        "path1": divergence and (s1 or s2) and p_any,
        "path2": reverse,
        "h1_relative": str(inputs.get("h1_relative", "unknown")),
    }


def extract_turn_events(
    rows: Sequence[Mapping[str, object]],
    reference: Sequence[int | None],
    *,
    csymbol: str,
) -> list[dict[str, object]]:
    """Extract the exact turn events used by the pre-registered delay metric."""

    if len(rows) != len(reference):
        raise ValueError("rows and reference must have equal length")
    predicted = [_state_value(row) for row in rows]
    events: list[dict[str, object]] = []
    prior_direction: int | None = None
    names = {1: "bull", -1: "bear"}
    for index, current in enumerate(reference):
        if current not in (-1, 1):
            continue
        if prior_direction is not None and current != prior_direction:
            target = names[current]
            resolved_at: int | None = None
            last_candidate = min(len(rows) - 2, index + 48)
            for candidate in range(index, last_candidate + 1):
                if predicted[candidate] == target and predicted[candidate + 1] == target:
                    resolved_at = candidate
                    break
            window_end = resolved_at if resolved_at is not None else min(len(rows) - 1, index + 48)
            window = rows[index : window_end + 1]
            actions = Counter(_decision_value(row, "proposed_action") for row in window)
            reasons = Counter(_decision_value(row, "reason_code") for row in window)
            h1_states = Counter(str(row.get("h1_absolute", "unknown")) for row in window)
            state_at_turn = predicted[index]
            delay = resolved_at - index if resolved_at is not None else 49
            events.append(
                {
                    "csymbol": csymbol,
                    "turn_eob": str(rows[index].get("eob", "")),
                    "from_direction": names[prior_direction],
                    "to_direction": target,
                    "delay_bars": delay,
                    "resolved": resolved_at is not None,
                    "resolved_eob": (
                        str(rows[resolved_at].get("eob", "")) if resolved_at is not None else None
                    ),
                    "state_at_turn": state_at_turn,
                    "action_at_turn": _decision_value(rows[index], "proposed_action"),
                    "reason_at_turn": _decision_value(rows[index], "reason_code"),
                    "h1_at_turn": str(rows[index].get("h1_absolute", "unknown")),
                    "leave_inputs_at_turn": _leave_inputs(rows[index], names[prior_direction]),
                    "window_bars": len(window),
                    "window_action_counts": dict(sorted(actions.items())),
                    "window_reason_counts": dict(sorted(reasons.items())),
                    "window_h1_counts": dict(sorted(h1_states.items())),
                }
            )
        prior_direction = current
    return events


def _bucket(delay: int) -> str:
    if delay <= 4:
        return "0-4"
    if delay <= 12:
        return "5-12"
    if delay <= 24:
        return "13-24"
    if delay <= 48:
        return "25-48"
    return "unresolved"


def summarize_turn_events(events: Sequence[Mapping[str, object]]) -> dict[str, object]:
    delays = [int(event["delay_bars"]) for event in events]
    delayed = [event for event in events if int(event["delay_bars"]) > 4]
    buckets = Counter(_bucket(int(event["delay_bars"])) for event in events)
    at_turn_states = Counter(str(event["state_at_turn"]) for event in delayed)
    at_turn_actions = Counter(str(event["action_at_turn"]) for event in delayed)
    at_turn_reasons = Counter(str(event["reason_at_turn"]) for event in delayed)
    at_turn_h1 = Counter(str(event["h1_at_turn"]) for event in delayed)
    state_relations: Counter[str] = Counter()
    h1_relations: Counter[str] = Counter()
    leave_input_true: Counter[str] = Counter()
    leave_input_available = 0
    old_state_leave_input_true: Counter[str] = Counter()
    old_state_leave_input_available = 0
    window_actions: Counter[str] = Counter()
    window_reasons: Counter[str] = Counter()
    window_h1: Counter[str] = Counter()
    events_with_veto = 0
    events_with_maintain = 0
    for event in delayed:
        state = str(event["state_at_turn"])
        old_direction = str(event["from_direction"])
        new_direction = str(event["to_direction"])
        if state == old_direction:
            state_relations["old_direction"] += 1
        elif state == new_direction:
            state_relations["new_direction"] += 1
        elif state == "none":
            state_relations["none"] += 1
        else:
            state_relations["unknown"] += 1
        h1 = str(event["h1_at_turn"])
        if h1 == f"{old_direction}_strong":
            h1_relations["old_direction_strong"] += 1
        elif h1 == f"{new_direction}_strong":
            h1_relations["new_direction_strong"] += 1
        elif h1 == f"{old_direction}_weakening":
            h1_relations["old_direction_weakening"] += 1
        elif h1 == f"{new_direction}_weakening":
            h1_relations["new_direction_weakening"] += 1
        else:
            h1_relations[h1] += 1
        leave_inputs = event.get("leave_inputs_at_turn")
        if isinstance(leave_inputs, Mapping) and bool(leave_inputs.get("available", False)):
            leave_input_available += 1
            for key in ("divergence", "s1", "s2", "p_close", "p_macd", "p_any", "r_reverse", "path1", "path2"):
                leave_input_true[key] += int(bool(leave_inputs.get(key, False)))
            if state == old_direction:
                old_state_leave_input_available += 1
                for key in ("divergence", "s1", "s2", "p_close", "p_macd", "p_any", "r_reverse", "path1", "path2"):
                    old_state_leave_input_true[key] += int(bool(leave_inputs.get(key, False)))
        actions = event["window_action_counts"]
        reasons = event["window_reason_counts"]
        h1_states = event["window_h1_counts"]
        if not isinstance(actions, Mapping) or not isinstance(reasons, Mapping) or not isinstance(h1_states, Mapping):
            raise ValueError("turn event counters must be mappings")
        window_actions.update({str(key): int(value) for key, value in actions.items()})
        window_reasons.update({str(key): int(value) for key, value in reasons.items()})
        window_h1.update({str(key): int(value) for key, value in h1_states.items()})
        events_with_veto += int(int(actions.get("veto", 0)) > 0)
        events_with_maintain += int(int(actions.get("maintain", 0)) > 0)
    return {
        "turn_events": len(events),
        "delay_median": median(delays) if delays else 0.0,
        "unresolved_rate": (
            sum(delay == 49 for delay in delays) / len(delays) if delays else 0.0
        ),
        "delay_buckets": dict(sorted(buckets.items())),
        "delayed_over_4_bars": len(delayed),
        "delayed_at_turn_state_counts": dict(sorted(at_turn_states.items())),
        "delayed_at_turn_action_counts": dict(sorted(at_turn_actions.items())),
        "delayed_at_turn_reason_counts": dict(sorted(at_turn_reasons.items())),
        "delayed_at_turn_h1_counts": dict(sorted(at_turn_h1.items())),
        "delayed_at_turn_state_relation_counts": dict(sorted(state_relations.items())),
        "delayed_at_turn_h1_relation_counts": dict(sorted(h1_relations.items())),
        "delayed_at_turn_leave_inputs_available": leave_input_available,
        "delayed_at_turn_leave_input_true_counts": dict(sorted(leave_input_true.items())),
        "delayed_old_state_leave_inputs_available": old_state_leave_input_available,
        "delayed_old_state_leave_input_true_counts": dict(sorted(old_state_leave_input_true.items())),
        "delayed_window_action_counts": dict(sorted(window_actions.items())),
        "delayed_window_reason_counts": dict(sorted(window_reasons.items())),
        "delayed_window_h1_counts": dict(sorted(window_h1.items())),
        "delayed_events_with_any_veto": events_with_veto,
        "delayed_events_with_any_maintain": events_with_maintain,
    }


def build_turn_audit(
    facts: Iterable[Mapping[str, object]],
    m15_bars: Iterable[Bar],
    *,
    dataset_label: str,
) -> dict[str, object]:
    bar_index = {(bar.symbol, _eob_key(bar.eob)): bar for bar in m15_bars}
    grouped: dict[str, list[tuple[dict[str, object], Bar, float]]] = defaultdict(list)
    missing: list[tuple[str, str]] = []
    for raw in facts:
        row = dict(raw)
        symbol = str(row.get("symbol", ""))
        csymbol = str(row.get("csymbol", ""))
        eob = _eob_key(row.get("eob"))
        bar = bar_index.get((symbol, eob))
        if bar is None:
            missing.append((symbol, eob))
            continue
        atr = row.get("atr14")
        if not isinstance(atr, (int, float)) or float(atr) <= 0:
            continue
        grouped[csymbol].append((row, bar, float(atr)))
    if missing:
        preview = ", ".join(f"{symbol}@{eob}" for symbol, eob in missing[:3])
        raise ValueError(f"{len(missing)} fact rows have no matching M15 bar: {preview}")
    if not grouped:
        raise ValueError("turn audit requires complete trend observations")

    all_events: list[dict[str, object]] = []
    per_product: dict[str, object] = {}
    for csymbol, observations in sorted(grouped.items()):
        observations.sort(key=lambda item: item[1].eob)
        rows = [item[0] for item in observations]
        bars = [item[1] for item in observations]
        atr = [item[2] for item in observations]
        reference = future_48bar_3atr_zigzag(bars, atr)
        events = extract_turn_events(rows, reference, csymbol=csymbol)
        all_events.extend(events)
        per_product[csymbol] = summarize_turn_events(events)

    slowest = sorted(
        all_events,
        key=lambda event: (int(event["delay_bars"]), str(event["turn_eob"])),
        reverse=True,
    )[:24]
    return {
        "schema_version": 1,
        "purpose": "SPRINT2_ROLLING_180D_TURN_DELAY_AUDIT",
        "dataset_label": dataset_label,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "performance_interpretation_allowed": False,
        "pool": summarize_turn_events(all_events),
        "per_product": per_product,
        "slowest_examples": slowest,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit pre-registered trend turn delays")
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--dataset-label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = build_turn_audit(
        _load_facts(args.facts),
        _load_m15_bars(args.data_dir),
        dataset_label=args.dataset_label,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "turn_events": report["pool"]["turn_events"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
