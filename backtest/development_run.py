from __future__ import annotations

import argparse
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


def _load_events(root: Path, manifest: dict[str, object]) -> tuple[list[ReplayBar], dict[str, str]]:
    events: list[ReplayBar] = []
    identity: dict[str, str] = {}
    products = manifest["products"]
    if not isinstance(products, dict):
        raise ValueError("manifest products must be an object")
    for csymbol, item in sorted(products.items()):
        if not isinstance(item, dict):
            raise ValueError(f"invalid product manifest for {csymbol}")
        frame = pd.read_parquet(root / str(item["continuous_path"]))
        if "trading_date" not in frame.columns:
            raise ValueError(f"continuous data for {csymbol} lacks trading_date")
        records = frame.to_dict("records")
        bars = bars_from_records(records)
        if {bar.symbol for bar in bars} != {csymbol}:
            raise ValueError(f"continuous data identity mismatch for {csymbol}")
        identity[csymbol] = csymbol
        for bar, raw in zip(bars, records):
            events.append(ReplayBar(bar, date.fromisoformat(str(raw["trading_date"]))))
    return events, identity


def run_development_trend_layer(root: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    manifest = validate_dataset_manifest(root)
    if manifest.get("start_date") != "2022-10-01" or manifest.get("end_date") != "2024-12-31":
        raise ValueError("manifest does not match the frozen development export range")
    config = load_config(default_config_path())
    events, identity = _load_events(root, manifest)
    required = {product.csymbol for product in config.products}
    if set(identity) != required:
        raise ValueError(f"dataset universe mismatch: expected {sorted(required)}, got {sorted(identity)}")
    strategy = SharedCoreFactsStrategy(
        rules=config.raw["rules"],
        csymbol_by_symbol=identity,
    )
    replay = FactsOnlyRunner(strategy_step=strategy, history_limit=1600).run(events)
    selected_facts: list[dict[str, object]] = []
    selected_bars = []
    for record, bar, trading_date in zip(replay.facts, replay.m15_bars, replay.trading_dates):
        if not DEVELOPMENT_START <= trading_date <= DEVELOPMENT_END:
            continue
        values = dict(record.values)
        if "trend_decision" not in values:
            continue
        selected_facts.append(values)
        selected_bars.append(bar)
    report = build_trend_report(
        selected_facts,
        selected_bars,
        dataset_label="development-2023-2024",
        official_development=True,
    )
    report["blocked_at"] = blocked_distribution(replay.blocked_at)
    report["source_manifest"] = "manifest.json"
    return report, selected_facts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the frozen Sprint 2 development trend layer")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    args = parser.parse_args(argv)
    report, facts = run_development_trend_layer(args.data_root)
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
