"""Verify raw journal chains and export a price-free collection status."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import check_lock, write


def inspect_journal(path):
    previous="0"*64
    kinds=Counter()
    symbols=Counter()
    errors=Counter()
    first_bar=last_bar=None
    last_seq=0
    last_coverage={}
    failure=None
    with path.open("rb") as stream:
        for number,line in enumerate(stream,1):
            try:
                event=json.loads(line)
                if event["seq"]!=number or event["previous_sha256"]!=previous:
                    raise ValueError("chain mismatch")
                if not line.endswith(b"\n"):
                    raise ValueError("partial final record")
            except (ValueError,KeyError) as exc:
                failure=dict(line=number,reason=str(exc))
                break
            previous=hashlib.sha256(line).hexdigest()
            kinds[event["kind"]]+=1
            last_seq=number
            if event["kind"]=="BAR":
                symbol=event["payload"]["symbol"]
                symbols[symbol]+=1
                first_bar=first_bar or event["received_at"]
                last_bar=event["received_at"]
            if event["kind"]=="COVERAGE":
                last_coverage=event["payload"]
            if event["kind"]=="SDK_ERROR":
                errors[str(event["payload"]["code"])]+=1
            if event["kind"]=="SUBSCRIBE_ERROR" and "2003" in event["payload"].get("message",""):
                errors["2003"]+=1
    return dict(records_verified=last_seq,chain_failure=failure,last_verified_line_sha256=previous,
                events=dict(kinds),callbacks_by_contract=dict(symbols),first_callback_at=first_bar,
                last_callback_at=last_bar,subscription_error_counts=dict(errors),last_coverage=last_coverage)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--report",type=Path,required=True)
    a=p.parse_args()
    lock=check_lock()
    snapshots=[]
    for path in sorted(a.root.glob("*/raw.jsonl")):
        item=inspect_journal(path)
        item["run_id"]=path.parent.name
        snapshots.append(item)
    report=dict(version="V5_RAW_GM_EVIDENCE",purpose="COLLECTION_HEALTH_ONLY",frozen_code_sha256=lock["code_sha256"],
                audited_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                fixed_first_day="2026-11-05",first_open="2026-11-04T21:00:00+08:00",
                planned_end="2026-11-05T15:05:00+08:00",runs=snapshots,
                supervisor=json.loads((a.root/"supervisor.json").read_text(encoding="utf8")),
                source_review="PENDING_INDEPENDENT_REVIEW",qualification="PENDING_SOURCE",
                quota_issue="GM2003 / asynchronous1202; full pool coverage not established")
    write(a.report,report)
    print(json.dumps(dict(runs=len(snapshots),last=snapshots[-1] if snapshots else None),ensure_ascii=False))


if __name__=="__main__":
    main()
