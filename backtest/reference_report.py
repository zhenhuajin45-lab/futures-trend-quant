from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from futures_trend_quant.core.models import Bar
from futures_trend_quant.core.facts.pipeline import trading_day_for_bar

from .metrics import future_48bar_3atr_first_touch, score_legacy_first_touch_metrics
from .reference_events import (
    atr_directional_change_reference,
    score_confirmed_turn_events,
    summarize_scored_event_details,
)
from .trend_report import _eob_key, _load_facts, _load_m15_bars, _predicted_state


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

LEGACY_EVENT_KEYS = (
    "turn_events",
    "turn_delay_median",
    "turn_delay_p75",
    "turn_delay_p90",
    "unresolved_turn_rate",
    "turn_delay_passed",
)


def _select(values: Mapping[str, object], keys: Sequence[str]) -> dict[str, object]:
    return {key: values[key] for key in keys}


def build_reference_comparison(
    facts: Iterable[Mapping[str, object]],
    m15_bars: Iterable[Bar],
    *,
    dataset_label: str,
    code_sha: str,
) -> dict[str, object]:
    """Compare legacy label changes with frozen independent turn events."""

    bar_index = {(bar.symbol, _eob_key(bar.eob)): bar for bar in m15_bars}
    grouped: dict[str, list[tuple[dict[str, object], Bar, float, str]]] = defaultdict(list)
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
        grouped[csymbol].append((row, bar, float(atr), _predicted_state(row)))
    if missing:
        preview = ", ".join(f"{symbol}@{eob}" for symbol, eob in missing[:3])
        raise ValueError(f"{len(missing)} fact rows have no matching M15 bar: {preview}")
    if not grouped:
        raise ValueError("reference comparison requires complete observations")

    per_product: dict[str, object] = {}
    pool_bars: list[Bar] = []
    pool_atr: list[float] = []
    pool_predicted: list[str] = []
    pool_reference: list[int | None] = []
    pool_dates: list[str] = []
    pool_symbols: list[str] = []
    pool_event_details: list[dict[str, object]] = []
    for csymbol, observations in sorted(grouped.items()):
        observations.sort(key=lambda item: item[1].eob)
        rows = [item[0] for item in observations]
        bars = [item[1] for item in observations]
        atr = [item[2] for item in observations]
        predicted = [item[3] for item in observations]
        trading_dates = [trading_day_for_bar(bar).isoformat() for bar in bars]
        first_touch = future_48bar_3atr_first_touch(bars, atr)
        legacy = score_legacy_first_touch_metrics(
            bars,
            atr,
            predicted,
            first_touch,
            trading_dates,
            symbols=[csymbol] * len(bars),
        )
        event_reference = atr_directional_change_reference(bars, atr)
        event_score = score_confirmed_turn_events(predicted, event_reference.events)
        event_details = list(event_score.pop("events"))
        for detail in event_details:
            detail["csymbol"] = csymbol
        pool_event_details.extend(event_details)
        per_product[csymbol] = {
            "observations": len(bars),
            "bar_reference": _select(legacy, BAR_KEYS),
            "legacy_label_change_events": _select(legacy, LEGACY_EVENT_KEYS),
            "independent_turn_events": {**event_score, "events": event_details},
            "initial_direction": event_reference.initial_direction,
            "initial_confirmation_index": event_reference.initial_confirmation_index,
        }
        pool_bars.extend(bars)
        pool_atr.extend(atr)
        pool_predicted.extend(predicted)
        pool_reference.extend(first_touch)
        pool_dates.extend(trading_dates)
        pool_symbols.extend([csymbol] * len(bars))

    pool_legacy = score_legacy_first_touch_metrics(
        pool_bars,
        pool_atr,
        pool_predicted,
        pool_reference,
        pool_dates,
        symbols=pool_symbols,
    )
    pool_events = summarize_scored_event_details(pool_event_details)
    return {
        "schema_version": 1,
        "purpose": "SPRINT2_P0B_REFERENCE_DEFINITION_COMPARISON",
        "report_status": "ROLLING_180D_RESEARCH",
        "dataset_label": dataset_label,
        "code_sha": code_sha,
        "performance_interpretation_allowed": False,
        "strategy_future_information_access": False,
        "bar_reference_definition": "FUTURE_FIRST_TOUCH_V1",
        "event_reference_definition": "ATR_DIRECTIONAL_CHANGE_V1",
        "legacy_event_definition": "LEGACY_FIRST_TOUCH_LABEL_CHANGE",
        "products": sorted(grouped),
        "observations": len(pool_bars),
        "pool": {
            "bar_reference": _select(pool_legacy, BAR_KEYS),
            "legacy_label_change_events": _select(pool_legacy, LEGACY_EVENT_KEYS),
            "independent_turn_events": {**pool_events, "events": pool_event_details},
        },
        "per_product": per_product,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare frozen Sprint 2 reference definitions")
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--dataset-label", required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = build_reference_comparison(
        _load_facts(args.facts),
        _load_m15_bars(args.data_dir),
        dataset_label=args.dataset_label,
        code_sha=args.code_sha,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output),
        "observations": report["observations"],
        "independent_turn_events": report["pool"]["independent_turn_events"]["turn_events"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
