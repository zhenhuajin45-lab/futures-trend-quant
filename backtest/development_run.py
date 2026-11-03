from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
import json
from pathlib import Path
from typing import Sequence

import pandas as pd

from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.data.development_export import validate_dataset_manifest
from futures_trend_quant.data.gm_bars import bars_from_records

from .facts_runner import FactsOnlyRunner
from .metrics import blocked_distribution
from .runner import ReplayBar
from .strategy import SharedCoreFactsStrategy
from .trend_report import build_trend_report


DEVELOPMENT_START = date(2023, 1, 1)
DEVELOPMENT_END = date(2024, 12, 31)
ROLLING_START = date(2026, 5, 7)
ROLLING_END = date(2026, 11, 2)


def _run_product(
    root_text: str,
    continuous_path: str,
    csymbol: str,
    rules: dict[str, object],
    history_limit: int,
    sample_start: date,
    sample_end: date,
) -> tuple[str, list[dict[str, object]], list[object], dict[str, int], int]:
    frame = pd.read_parquet(Path(root_text) / continuous_path)
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
    strategy = SharedCoreFactsStrategy(
        rules=rules,
        csymbol_by_symbol={csymbol: csymbol},
    )
    replay = FactsOnlyRunner(
        strategy_step=strategy,
        history_limit=history_limit,
    ).run(events)
    selected_facts: list[dict[str, object]] = []
    selected_bars: list[object] = []
    for record, bar, trading_date in zip(replay.facts, replay.m15_bars, replay.trading_dates):
        if not sample_start <= trading_date <= sample_end:
            continue
        values = dict(record.values)
        if "trend_decision" not in values:
            continue
        selected_facts.append(values)
        selected_bars.append(bar)
    return (
        csymbol,
        selected_facts,
        selected_bars,
        blocked_distribution(replay.blocked_at),
        len(replay.facts),
    )


def _run_trend_layer(
    root: Path,
    *,
    sample_start: date,
    sample_end: date,
    purpose: str,
    dataset_label: str,
    official_development: bool,
    workers: int = 1,
    rules_override: dict[str, object] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    manifest = validate_dataset_manifest(root)
    if manifest.get("purpose") != purpose:
        raise ValueError(f"manifest purpose mismatch: expected {purpose}")
    config = load_config(default_config_path())
    rules = dict(config.raw["rules"] if rules_override is None else rules_override)
    products = manifest.get("products")
    if not isinstance(products, dict):
        raise ValueError("manifest products must be an object")
    required = {product.csymbol for product in config.products}
    if set(products) != required:
        raise ValueError(f"dataset universe mismatch: expected {sorted(required)}, got {sorted(products)}")
    tasks = [
        (
            str(root),
            str(item["continuous_path"]),
            csymbol,
            rules,
            config.subscription_count,
            sample_start,
            sample_end,
        )
        for csymbol, item in sorted(products.items())
        if isinstance(item, dict)
    ]
    if len(tasks) != len(products):
        raise ValueError("manifest contains invalid product entries")
    product_results = []
    if workers <= 1:
        for task in tasks:
            product_results.append(_run_product(*task))
    else:
        with ProcessPoolExecutor(max_workers=min(workers, len(tasks))) as executor:
            futures = {executor.submit(_run_product, *task): task[2] for task in tasks}
            for future in as_completed(futures):
                result = future.result()
                product_results.append(result)
                print(json.dumps({
                    "event": "TREND_REPLAY_PRODUCT_DONE",
                    "csymbol": result[0],
                    "complete_facts": len(result[1]),
                    "m15_callbacks": result[4],
                }, ensure_ascii=False), flush=True)
    selected_facts: list[dict[str, object]] = []
    selected_bars: list[object] = []
    blocked_rows: list[str] = []
    callback_count = 0
    for _, facts, bars, blocked, callbacks in sorted(product_results):
        selected_facts.extend(facts)
        selected_bars.extend(bars)
        callback_count += callbacks
        for reason, count in blocked.items():
            blocked_rows.extend([reason] * count)
    report = build_trend_report(
        selected_facts,
        selected_bars,
        dataset_label=dataset_label,
        official_development=official_development,
    )
    report["blocked_at"] = blocked_distribution(blocked_rows)
    report["m15_callbacks"] = callback_count
    report["workers"] = max(1, workers)
    report["source_manifest"] = "manifest.json"
    return report, selected_facts


def run_development_trend_layer(
    root: Path, *, workers: int = 1
) -> tuple[dict[str, object], list[dict[str, object]]]:
    manifest = validate_dataset_manifest(root)
    if manifest.get("start_date") != "2022-10-01" or manifest.get("end_date") != "2024-12-31":
        raise ValueError("manifest does not match the frozen development export range")
    return _run_trend_layer(
        root,
        sample_start=DEVELOPMENT_START,
        sample_end=DEVELOPMENT_END,
        purpose="SPRINT2_DEVELOPMENT_DATASET",
        dataset_label="development-2023-2024",
        official_development=True,
        workers=workers,
    )


def run_rolling_trend_layer(
    root: Path,
    *,
    workers: int = 1,
    rules_override: dict[str, object] | None = None,
    dataset_label: str = "rolling-180d-20260507-20261102",
) -> tuple[dict[str, object], list[dict[str, object]]]:
    manifest = validate_dataset_manifest(root)
    if manifest.get("start_date") != ROLLING_START.isoformat() or manifest.get("end_date") != ROLLING_END.isoformat():
        raise ValueError("manifest does not match the frozen rolling 180-day range")
    report, facts = _run_trend_layer(
        root,
        sample_start=ROLLING_START,
        sample_end=ROLLING_END,
        purpose="SPRINT2_ROLLING_180D",
        dataset_label=dataset_label,
        official_development=False,
        workers=workers,
        rules_override=rules_override,
    )
    report["report_status"] = "ROLLING_180D_RESEARCH"
    return report, facts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the frozen Sprint 2 development trend layer")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--mode", choices=("development", "rolling180"), default="development")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    if args.mode == "rolling180":
        report, facts = run_rolling_trend_layer(args.data_root, workers=args.workers)
    else:
        report, facts = run_development_trend_layer(args.data_root, workers=args.workers)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.facts.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with args.facts.open("w", encoding="utf-8", newline="\n") as handle:
        for row in facts:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"report": str(args.report), "facts": len(facts)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
