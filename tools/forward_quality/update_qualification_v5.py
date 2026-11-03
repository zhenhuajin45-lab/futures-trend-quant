"""Attach collection-request evidence to the unchanged historical qualification table."""
import csv
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def main():
    status=json.loads((ROOT/"reports/v5_collection_status.json").read_text(encoding="utf8"))
    latest=status["runs"][-1]
    coverage=latest["last_coverage"]
    accepted=coverage["subscribed"]
    callbacks=latest["callbacks_by_contract"]
    source=ROOT/"reports/v4_warmup_qualification.csv"
    target=ROOT/"reports/v5_warmup_qualification.csv"
    with source.open(encoding="utf-8-sig",newline="") as file:
        rows=list(csv.DictReader(file))
    for row in rows:
        prefix=row["csymbol"].upper()
        matches=[s for s in coverage["main_contracts"] if "".join(x for x in s if not x.isdigit()).upper()==prefix]
        actual=matches[0] if matches else "UNKNOWN"
        row.update(actual_main=actual, subscription_request_accepted=str(actual in accepted),
                   live_callbacks_current_run=str(callbacks.get(actual,0)), collector_run=latest["run_id"],
                   historical_qualification="UNKNOWN",source_status="UNKNOWN",
                   next_action="GM subscription entitlement and historical source evidence",
                   due_at="2026-11-06 internal review; supplier delivery unconfirmed")
    with target.open("w",encoding="utf-8-sig",newline="") as file:
        writer=csv.DictWriter(file,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__=="__main__":
    main()
