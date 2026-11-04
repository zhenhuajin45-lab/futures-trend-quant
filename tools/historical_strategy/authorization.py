"""T1 supplement: actual S0 authorization episodes, separate from reference-pair timing."""
import argparse
from bisect import bisect_right
from collections import defaultdict
import gzip
import json
from pathlib import Path
import sys
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,stamp,digest
from tools.historical_strategy.inventory import contract_quality
from tools.historical_strategy.price_paths import ExpectedClock
from tools.historical_strategy.analyze import distribution


def space(time,close,atr,direction,raw,clock,horizon):
    times,full=clock.window(time,horizon)
    missing=next((t for t in times if t not in raw),None)
    if not full or missing is not None:
        return dict(complete=False,first_missing=None if missing is None else missing.isoformat())
    rows=[raw[t] for t in times]
    sign=1 if direction=="bull" else -1
    return dict(complete=True,mfe_atr=max(0.,max(sign*((r["high"] if sign>0 else r["low"])-close) for r in rows))/atr,
        mae_atr=max(0.,max(sign*(close-(r["low"] if sign>0 else r["high"])) for r in rows))/atr)


def main():
    p=argparse.ArgumentParser()
    for name in ("data-root","replay-root","analysis-root","summary"):
        p.add_argument("--"+name,type=Path,required=True)
    a=p.parse_args()
    all_ops=read(a.analysis_root/"opportunities.json")
    by_contract=defaultdict(list)
    for o in all_ops:
        by_contract[o["actual_contract"]].append(o)
    records=[]
    for task in read(a.data_root/"manifest.json")["tasks"]:
        frame=pd.read_parquet(a.data_root/task["path"])
        raw={r["eob"]:r for r in frame.to_dict("records")}
        expected=[stamp(x["eob"]) for x in read(a.data_root/(task["csymbol"]+"_schedule.json"))]
        quality,_=contract_quality(frame,expected)
        clock=ExpectedClock(expected)
        with gzip.open(a.replay_root/(task["symbol"]+".jsonl.gz"),"rt",encoding="utf8") as stream:
            header=json.loads(next(stream))
            assert header["input_sha256"]==digest(a.data_root/task["path"])
            traces=[json.loads(line) for line in stream]
        previous=None
        episode=0
        for i,row in enumerate(traces):
            core=row["groups"]["S0"]
            state=core["trend_decision"]["state"]["trend_state"]
            changed=state!=previous
            episode+=changed
            prior=previous
            previous=state
            if not(changed and state in ("bull","bear") and row["selected"] and quality.get(stamp(row["bar"]["eob"]),False)):
                continue
            t=stamp(row["decision_eob"])
            following,_=clock.window(t,48)
            last=following[-1] if following else t
            op=next((o for o in by_contract[task["symbol"]] if o["episode"]==episode and o["i"]>=i and stamp(o["time"])<=last),None)
            delay=None if op is None else bisect_right(clock.boundaries,stamp(op["time"]))-bisect_right(clock.boundaries,t)
            record=dict(csymbol=task["csymbol"],actual_contract=task["symbol"],authorization_eob=t.isoformat(),
                direction=state,prior_direction=prior,episode=episode,
                first_unique_valid_candidate=None if op is None else op["opportunity_id"],
                candidate_delay_m15=delay,
                candidate_wall_minutes=None if op is None else (stamp(op["time"])-t).total_seconds()/60,
                future={str(h):space(t,core["close"],core["atr14"],state,raw,clock,h) for h in (8,48)})
            if op is not None:
                record["candidate_future"]={str(h):space(stamp(op["time"]),op["close"],op["atr"],state,raw,clock,h) for h in (8,48)}
            records.append(record)
    summaries={}
    for h in ("8","48"):
        complete=[r for r in records if r["future"][h]["complete"]]
        common=[r for r in complete if r.get("candidate_future",{}).get(h,{}).get("complete")]
        summaries[h]=dict(authorized_episodes=len(records),complete_future=len(complete),
            with_unique_valid_candidate=sum(r["first_unique_valid_candidate"] is not None for r in complete),
            mfe_atr=distribution(r["future"][h]["mfe_atr"] for r in complete),
            mae_atr=distribution(r["future"][h]["mae_atr"] for r in complete),
            future_3atr_reached=sum(r["future"][h]["mfe_atr"]>=3 for r in complete),
            paired_authorization_candidate_n=len(common),
            paired_authorization_mfe_atr=distribution(r["future"][h]["mfe_atr"] for r in common),
            paired_candidate_mfe_atr=distribution(r["candidate_future"][h]["mfe_atr"] for r in common),
            candidate_delay_m15=distribution(r["candidate_delay_m15"] for r in common))
    write(a.summary,dict(study="ACTUAL_S0_AUTHORIZATION_SPACE",summaries=summaries,events=records,
        source_scope="NEW_DIRECTION_TRANSITIONS_INSIDE_QUALIFIED_SELECTED_SAMPLE; INHERITED_INITIAL_STATES_EXCLUDED",
        price_anchor="EACH_CLOSED_M15_CLOSE; NO_EXECUTION_CLAIM",future_can_include_later_reversals=True,
        no_strategy_variant=True))
    print("AUTHORIZATION_SPACE_COMPLETE",len(records),flush=True)


if __name__=="__main__":
    main()
