"""Read schema evidence without upgrading missing historical source clocks."""
import argparse
from pathlib import Path
import json
import sys
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from backtest.forward_v3 import check_lock, digest, write


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--gm-root",type=Path,required=True)
    p.add_argument("--study",type=Path,required=True)
    p.add_argument("--report",type=Path,required=True)
    p.add_argument("--calendar-basis",type=Path)
    a=p.parse_args()
    lock=check_lock()
    manifest=json.loads((a.gm_root/"manifest.json").read_text(encoding="utf8"))
    rows=[]
    for symbol in lock["universe"]:
        product=manifest["products"][symbol]
        mapping=a.gm_root/product["mapping_path"]
        raw=[a.gm_root/r["path"] for r in product["raw_files"]]
        schemas=[set(pq.read_schema(file).names) for file in raw]
        rows.append(dict(csymbol=symbol, mapping_sha256=digest(mapping), raw_file_count=len(raw),
                         mapping_has_known_at="known_at" in pq.read_schema(mapping).names,
                         raw_files_with_available_at=sum("available_at" in s for s in schemas),
                         raw_files_with_decision_at=sum("decision_at" in s for s in schemas),
                         source_status="UNKNOWN", warmup_valid_m15=None,
                         reason="HISTORICAL_CLOCK_EVIDENCE_NOT_IN_EXPORTED_CACHE",
                         owner="Codex 数据负责人", due_at=None))
    sealed=list((a.study/"days").glob("????-??-??"))
    report=dict(status="PENDING_SOURCE", source_verification="UNCONFIRMED",
                fixed_first_day=None, fixed_first_day_reason="INDEPENDENT_FUTURES_CALENDAR_NOT_DELIVERED",
                owner="Codex 数据负责人",decision_kind="DRAFT_FOR_PROJECT_OWNER",
                received_score_days=0,sealed_score_days=len(sealed),proven_score_days=0,
                historical_cache_manifest_sha256=digest(a.gm_root/"manifest.json"),
                clock_evidence_recoverability="NOT_ESTABLISHED",products=rows,
                frozen_code_sha256=lock["code_sha256"],orders="OFFLINE_ONLY",
                source_followup_due="2026-11-06",followup_basis="3 workday evidence budget; no promised supplier delivery",
                first_day_close_status="UNKNOWN_UNTIL_AUTHORIZED_CALENDAR")
    if a.calendar_basis:
        basis=json.loads(a.calendar_basis.read_text(encoding="utf8"))
        report.update(fixed_first_day=basis["fixed_first_day"],first_day_close_status="AWAITING_CLOSE",
                      fixed_first_day_reason="AUTHORIZED_INDEPENDENT_CALENDAR_AND_SESSION_SNAPSHOT",
                      calendar_basis_sha256=digest(a.calendar_basis))
        for row in rows:
            row.update(fixed_first_day=basis["fixed_first_day"],calendar_version=basis["source_snapshot_sha256"],
                       warmup_from="UNKNOWN",warmup_to="UNKNOWN",
                       mapping_clock_source="NOT_DELIVERED",quote_clock_source="NOT_DELIVERED",
                       decision_clock_mode="UNCONFIRMED",day1_expected_m1=basis["expected_m1"][row["csymbol"]],
                       day1_received_any_leg_m1=None,day1_strict_valid_m1=None,first_bad_eob=None,
                       evidence_ref="v4_source_inventory.json / cache manifest schemas")
    import csv
    columns=list(rows[0])
    with a.report.with_name("v4_warmup_qualification.csv").open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    write(a.report,report)
    print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__":
    main()
