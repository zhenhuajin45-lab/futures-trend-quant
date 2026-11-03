from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Mapping, Sequence

from futures_trend_quant.config import default_config_path, load_config

from .ablation import generate_sprint2_cases
from .development_run import run_rolling_trend_layer


TREND_AFFECTING_KEYS = {
    "rules.div_mode",
    "rules.swing_mode",
    "rules.h1_veto_enabled",
    "rules.h1_strong_atr",
}


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")


def _matrix_row(
    *,
    name: str,
    key_path: str,
    baseline: object,
    candidate: object,
    status: str,
    report: Mapping[str, object],
) -> dict[str, object]:
    pool = report.get("pool")
    if not isinstance(pool, Mapping):
        raise ValueError(f"trend report for {name} has no pool metrics")
    return {
        "case": name,
        "key_path": key_path,
        "baseline": baseline,
        "candidate": candidate,
        "trend_execution_status": status,
        "trade_layer_status": "BLOCKED_UNVERIFIED_FEES",
        "fact_rows": report.get("fact_rows"),
        "direction_agreement": pool.get("direction_agreement"),
        "direction_agreement_passed": pool.get("direction_agreement_passed"),
        "trend_segment_coverage": pool.get("trend_segment_coverage"),
        "trend_segment_coverage_passed": pool.get("trend_segment_coverage_passed"),
        "range_switches_per_product_day": pool.get("range_switches_per_product_day"),
        "range_switches_passed": pool.get("range_switches_passed"),
        "turn_delay_median": pool.get("turn_delay_median"),
        "turn_delay_passed": pool.get("turn_delay_passed"),
        "unresolved_turn_rate": pool.get("unresolved_turn_rate"),
    }


def run_rolling_ablation(
    *,
    data_root: Path,
    baseline_path: Path,
    report_dir: Path,
    workers: int,
) -> dict[str, object]:
    config = load_config(default_config_path())
    baseline_report = json.loads(baseline_path.read_text(encoding="utf-8"))
    rows = [
        _matrix_row(
            name="BASELINE",
            key_path="",
            baseline=None,
            candidate=None,
            status="REPLAYED",
            report=baseline_report,
        )
    ]
    report_dir.mkdir(parents=True, exist_ok=True)
    for case in generate_sprint2_cases(config.raw):
        if case.key_path not in TREND_AFFECTING_KEYS:
            rows.append(
                _matrix_row(
                    name=case.name,
                    key_path=case.key_path,
                    baseline=case.baseline,
                    candidate=case.candidate,
                    status="TREND_INVARIANT_BY_DESIGN",
                    report=baseline_report,
                )
            )
            continue
        case_path = report_dir / f"{_safe_name(case.name)}.json"
        if case_path.exists():
            report = json.loads(case_path.read_text(encoding="utf-8"))
            status = "REUSED_CHECKPOINT"
        else:
            report, _ = run_rolling_trend_layer(
                data_root,
                workers=workers,
                rules_override=dict(case.config["rules"]),
                dataset_label=f"rolling-180d-20260507-20261102__{case.name}",
            )
            report["ablation"] = {
                "case": case.name,
                "key_path": case.key_path,
                "baseline": case.baseline,
                "candidate": case.candidate,
            }
            case_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            status = "REPLAYED"
        rows.append(
            _matrix_row(
                name=case.name,
                key_path=case.key_path,
                baseline=case.baseline,
                candidate=case.candidate,
                status=status,
                report=report,
            )
        )
    return {
        "schema_version": 1,
        "purpose": "SPRINT2_ROLLING_180D_ONE_AT_A_TIME_ABLATION",
        "performance_interpretation_allowed": False,
        "dataset_label": "rolling-180d-20260507-20261102",
        "trend_affecting_keys": sorted(TREND_AFFECTING_KEYS),
        "cases": rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run checkpointed rolling 180-day ablations")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    matrix = run_rolling_ablation(
        data_root=args.data_root,
        baseline_path=args.baseline,
        report_dir=args.report_dir,
        workers=args.workers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "cases": len(matrix["cases"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
