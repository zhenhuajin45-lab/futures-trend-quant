"""Single entry point for the locked V6 historical study; no live data or orders."""
import argparse
import gzip
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,digest,check_lock


def canonical_digest(path):
    return hashlib.sha256(path.read_text(encoding="utf8").encode("utf8")).hexdigest()


def trace_digest(path):
    result=hashlib.sha256()
    with gzip.open(path,"rt",encoding="utf8") as stream:
        for line in stream:
            result.update(line.encode("utf8"))
    return result.hexdigest()


def check_sources(lock,root=ROOT):
    for name,expected in lock["canonical_source_sha256"].items():
        if canonical_digest(root/name)!=expected:
            raise ValueError("study source changed; register a new study and rebuild replay cache: "+name)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data-root",type=Path,default=ROOT/"data/cache/history_v6_unadjusted")
    p.add_argument("--replay-root",type=Path,default=ROOT/"data/cache/history_v6_replay")
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--check-only",action="store_true")
    a=p.parse_args()
    lock=read(ROOT/"reports/v6_reproduction_lock.json")
    check_lock()
    check_sources(lock)
    if digest(a.data_root/"manifest.json")!=lock["data_manifest_sha256"]:
        raise ValueError("historical data view changed; do not silently reuse V6 labels")
    for name,expected in lock["data_file_sha256"].items():
        if digest(a.data_root/name)!=expected:
            raise ValueError("historical contract file changed: "+name)
    for name,expected in lock["replay_jsonl_sha256"].items():
        if trace_digest(a.replay_root/name)!=expected:
            raise ValueError("replay snapshot changed: "+name)
    if a.check_only:
        print("V6_SOURCE_DATA_REPLAY_LOCK_PASS",flush=True)
        return
    a.output.mkdir(parents=True,exist_ok=False)
    command=[sys.executable,str(ROOT/"tools/historical_strategy/analyze.py"),"--data-root",str(a.data_root),
             "--replay-root",str(a.replay_root),"--local-output",str(a.output/"private"),"--summary",str(a.output/"summary.json")]
    completed=subprocess.run(command,cwd=ROOT,check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)
    actual=read(a.output/"summary.json")
    expected=read(ROOT/"reports/v6_price_events_summary.json")
    keys=("opportunities","episodes","repeated_rows","matching_coverage","complete_matched_pairs","signals",
          "controls","pair_comparison","short_pair_comparison","fast_reference_legs","trend_exit_comparison")
    for key in keys:
        if actual[key]!=expected[key]:
            raise AssertionError("historical reproduction mismatch: "+key)
    def signature(row):
        return (row["time"],row["matched"],tuple((h,tuple(row["labels"][h].get(field) for field in
            ("label","complete","entry_eob","first_touch_eob","first_touch_point"))) for h in ("8","48")))
    actual_rows=read(a.output/"private/opportunities.json")
    baseline_rows=read(ROOT/"reports/v6_opportunity_pairs.json")["events"]
    if {r["opportunity_id"]:signature(r) for r in actual_rows}!={r["opportunity_id"]:signature(r) for r in baseline_rows}:
        raise AssertionError("per-opportunity identity or first-touch sequence changed")
    write(a.output/"reproduction_check.json",dict(pass_all=True,compared_fields=list(keys),no_live_api=True,
        per_opportunity_keys_and_touch_order_equal=True,unique_opportunities_checked=len(actual_rows),
        no_orders=True,expected_summary_sha256=digest(ROOT/"reports/v6_price_events_summary.json")))
    print("V6_REPRODUCTION_PASS",actual["opportunities"],actual["complete_matched_pairs"],flush=True)


if __name__=="__main__":
    main()
