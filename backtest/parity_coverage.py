from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Sequence

from .parity import DEFAULT_FACT_KEYS


def build_parity_coverage(
    gm_rows: Sequence[Mapping[str, object]],
    parity_summary: Mapping[str, object],
    *,
    code_sha: str,
) -> dict[str, object]:
    available = sorted(set().union(*(row.keys() for row in gm_rows))) if gm_rows else []
    return {
        "schema_version": 1,
        "purpose": "SPRINT2_P1D_GM_OFFLINE_PARITY_COVERAGE",
        "code_sha": code_sha,
        "sample": "2026-10-20_to_2026-10-23_10_products",
        "shared_core_caveat": "GM and offline paths share run_core_step; this proves adapter/replay parity for captured fields, not independent strategy correctness.",
        "row_identity": ["symbol", "eob"],
        "compared_fact_keys": list(DEFAULT_FACT_KEYS),
        "gm_artifact_available_keys": available,
        "rows": {
            "gm": int(parity_summary.get("gm_rows", 0)),
            "offline": int(parity_summary.get("offline_rows", 0)),
            "mismatches": int(parity_summary.get("mismatch_count", 0)),
            "acceptance_passed": bool(parity_summary.get("acceptance_passed", False)),
        },
        "coverage": [
            {"area": "row_identity_and_time_boundary", "status": "COMPARED", "evidence": "symbol/eob exact identity"},
            {"area": "price_and_indicators", "status": "COMPARED", "evidence": "close/ema34/atr14/slope_state"},
            {"area": "h1_and_divergence_summary", "status": "COMPARED", "evidence": "h1_absolute/top_div/bottom_div"},
            {"area": "trend_state_transition", "status": "COMPARED_AS_NESTED_OBJECT", "evidence": "trend_decision exact nested equality"},
            {"area": "h1_relative", "status": "CAPTURED_NOT_COMPARED", "evidence": "present in GM artifact but outside DEFAULT_FACT_KEYS"},
            {"area": "trend_inputs_S1_S2_P_R", "status": "NOT_CAPTURED_IN_GM_ARTIFACT", "evidence": "current facts_row field postdates artifact"},
            {"area": "path_candidates_ABCD", "status": "NOT_COMPARED", "evidence": "not present in M15_FACTS artifact"},
            {"area": "blocked_at_reason", "status": "NOT_COMPARED", "evidence": "not present in paired artifact"},
            {"area": "order_intent_and_sizing", "status": "NOT_COMPARED", "evidence": "no paired order-intent artifact"},
            {"area": "fills_partial_cancel_reject", "status": "NOT_COMPARED", "evidence": "no paired broker-event artifact"},
            {"area": "exit_event_and_realized_state", "status": "NOT_COMPARED", "evidence": "no paired exit artifact"},
        ],
        "performance_interpretation_allowed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Describe exact GM/offline parity coverage")
    parser.add_argument("--gm-facts", type=Path, required=True)
    parser.add_argument("--parity-summary", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    gm_rows = [
        json.loads(line)
        for line in args.gm_facts.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    parity_summary = json.loads(args.parity_summary.read_text(encoding="utf-8"))
    report = build_parity_coverage(gm_rows, parity_summary, code_sha=args.code_sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "rows": report["rows"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
