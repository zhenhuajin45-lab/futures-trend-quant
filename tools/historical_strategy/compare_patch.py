"""Compare same-time S0 facts and evidence with/without recovered target nights."""
import argparse
import gzip
import json
from pathlib import Path
import sys
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,stamp,digest
from tools.historical_strategy.analyze import extract
from tools.historical_strategy.inventory import contract_quality


def load(root,replay,task):
    path=root/task["path"]
    with gzip.open(replay/(task["symbol"]+".jsonl.gz"),"rt",encoding="utf8") as stream:
        header=json.loads(next(stream))
        assert header["input_sha256"]==digest(path)
        rows=[json.loads(x) for x in stream]
    expected=[stamp(x["eob"]) for x in read(root/(task["csymbol"]+"_schedule.json"))]
    quality,_=contract_quality(pd.read_parquet(path),expected)
    for row in rows:
        row["selected"]=row["selected"] and quality.get(stamp(row["bar"]["eob"]),False)
    return rows


def main():
    p=argparse.ArgumentParser()
    for name in ("data-root","replay-root","without-data-root","without-replay-root","summary"):
        p.add_argument("--"+name,type=Path,required=True)
    a=p.parse_args()
    main_manifest=read(a.data_root/"manifest.json")
    counter=read(a.without_data_root/"manifest.json")
    rules=read(ROOT/"config/strategy.json")["rules"]
    ticks=read(ROOT/"config/fees.json")["products"]
    cases=[]
    for task in counter["tasks"]:
        main_task=next(x for x in main_manifest["tasks"] if x["symbol"]==task["symbol"])
        with_rows=load(a.data_root,a.replay_root,main_task)
        without_rows=load(a.without_data_root,a.without_replay_root,task)
        left={r["bar"]["eob"]:r for r in with_rows}
        right={r["bar"]["eob"]:r for r in without_rows}
        changed={key:0 for key in ("ema34","atr14","slope_state","h1_absolute","top_div","bottom_div","trend_decision","entry_evaluation")}
        selected=0
        examples=[]
        for t in sorted(set(left)&set(right)):
            l,r=left[t],right[t]
            assert l["bar"]["close"]==r["bar"]["close"],"same-time actual-contract prices changed"
            if not(l["selected"] and r["selected"]):
                continue
            selected+=1
            fields=[]
            for key in changed:
                u,v=l["groups"]["S0"][key],r["groups"]["S0"][key]
                differs=abs(u-v)>1e-6 if key in ("ema34","atr14") else u!=v
                changed[key]+=int(differs)
                if differs:
                    fields.append(key)
            if fields and len(examples)<12:
                examples.append(dict(eob=t,changed_fields=fields,
                    with_trend=l["groups"]["S0"]["trend_decision"]["state"]["trend_state"],
                    without_trend=r["groups"]["S0"]["trend_decision"]["state"]["trend_state"]))
        tick=ticks[task["csymbol"].split(".")[1]]["price_tick"]
        lops,_,_,_=extract(main_task,with_rows,tick,rules)
        rops,_,_,_=extract(task,without_rows,tick,rules)
        li={o["opportunity_id"]:o for o in lops}
        ri={o["opportunity_id"]:o for o in rops}
        common=set(li)&set(ri)
        cases.append(dict(csymbol=task["csymbol"],actual_contract=task["symbol"],common_qualified_m15=selected,
            with_only_m15=len(set(left)-set(right)),without_only_m15=len(set(right)-set(left)),
            changed_fields=changed,first_changed_examples=examples,
            unique_evidence_with=len(li),unique_evidence_without=len(ri),common_evidence=len(common),
            evidence_added=len(set(li)-set(ri)),evidence_removed=len(set(ri)-set(li)),
            common_evidence_action_time_changed=sum(li[k]["time"]!=ri[k]["time"] for k in common)))
    write(a.summary,dict(cases=cases,numeric_tolerance=1e-6,only_target_recovered_nights_removed=True,
                         live_clock_status="UNKNOWN; NOT_UPGRADED_BY_RECOVERY"))


if __name__=="__main__":
    main()
