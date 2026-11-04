"""Independent actual-contract replay, shared frozen core functions, no account."""
import argparse
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date
import gzip
import json
from pathlib import Path
import sys

import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.aggregation import SequentialAggregator
from backtest.exit_experiment_strategy import ExitExperimentFactsStrategy
from backtest.runner import StepInput
from backtest.forward_v3 import digest, read, stamp
from futures_trend_quant.config import load_config
from futures_trend_quant.data.gm_bars import bars_from_records
from futures_trend_quant.core.facts.pipeline import analyze_market
from futures_trend_quant.core.facts_state import FactsStateBook, update_facts_state, latest_valid_divergence

GROUPS=("S0","A","EMA34_CONTROL")


def replay_contract(root_text, task, output_text):
    root,output=Path(root_text),Path(output_text)
    path=root/task["path"]
    target=output/(task["symbol"]+".jsonl.gz")
    if target.exists():
        with gzip.open(target,"rt",encoding="utf8") as file:
            header=json.loads(next(file))
        if header["input_sha256"]!=digest(path):
            raise ValueError("cached replay input changed")
        return task["symbol"],"CACHED"
    config=load_config(ROOT/"config/strategy.json")
    rules=config.raw["rules"]
    frame=pd.read_parquet(path)
    bars=bars_from_records(frame.to_dict("records"))
    selection=read(root/(task["csymbol"]+"_selection.json"))
    expected={stamp(row["eob"]):row["trading_date"] for row in read(root/(task["csymbol"]+"_schedule.json"))}
    expected_by_bucket={}
    for t in expected:
        expected_by_bucket.setdefault(t.ceil("15min"),set()).add(t)
    m1,m5,m15=deque(maxlen=1200),deque(maxlen=1200),deque(maxlen=1200)
    a5,a15=SequentialAggregator(5,"300s"),SequentialAggregator(15,"900s")
    facts=FactsStateBook().for_csymbol(task["symbol"])
    strategies={g:ExitExperimentFactsStrategy(g,rules,{task["symbol"]:task["symbol"]}) for g in GROUPS}
    temporary=target.with_suffix(".pending")
    n15=0
    observed_bucket=set()
    bucket=None
    count=selected=0
    with gzip.open(temporary,"wt",encoding="utf8") as out:
        out.write(json.dumps(dict(header=True,task=task,input_sha256=digest(path),
                                 registration_sha256=digest(ROOT/"tools/historical_strategy/registration.json")))+"\n")
        for m1_index,bar in enumerate(bars):
            m1.append(bar)
            t=stamp(bar.eob).ceil("15min")
            if bucket!=t:
                bucket=t
                observed_bucket=set()
            observed_bucket.add(stamp(bar.eob))
            b5=a5.update(bar)
            if b5 is not None:
                m5.append(b5)
            b15=a15.update(bar)
            if b15 is None:
                continue
            m15.append(b15)
            n15+=1
            try:
                history=list(m15)
                update_facts_state(facts,history,rules)
                analysis=analyze_market(history,rules)
            except ValueError as exc:
                if any(marker in str(exc).lower() for marker in ("warmup","unavailable","requires")):
                    continue
                raise
            day=str(expected[stamp(b15.eob)])
            event=StepInput(task["symbol"],date.fromisoformat(day),b15,tuple(m5),tuple(m15),None,tuple(m1),())
            top=latest_valid_divergence(facts,history,"top")
            bottom=latest_valid_divergence(facts,history,"bottom")
            shared={}
            groups={}
            for group,strategy in strategies.items():
                result=strategy.run_prepared(event,task["symbol"],strategy.runtime,analysis,top,bottom,shared)
                groups[group]=dict(result.facts)
            complete=(stamp(b15.eob)==stamp(bar.eob) and observed_bucket==expected_by_bucket[stamp(b15.eob)])
            is_selected=(n15>=1200 and complete and selection.get(day)==task["symbol"])
            value=dict(i15=n15-1,m1_index=m1_index,trading_date=day,selected=is_selected,
                       complete_m15=complete,decision_eob=bar.eob.isoformat(),bar=asdict(b15),groups=groups)
            out.write(json.dumps(value,default=str,ensure_ascii=False,allow_nan=False)+"\n")
            count+=1
            selected+=int(is_selected)
    temporary.replace(target)
    return task["symbol"],dict(rows=count,selected=selected)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data-root",type=Path,required=True)
    p.add_argument("--local-output",type=Path,required=True)
    p.add_argument("--products",default="")
    p.add_argument("--workers",type=int,default=4)
    a=p.parse_args()
    a.local_output.mkdir(exist_ok=True,parents=True)
    manifest=read(a.data_root/"manifest.json")
    products=set(a.products.split(",")) if a.products else set(manifest["products"])
    tasks=[task for task in manifest["tasks"] if task["csymbol"] in products]
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        futures=[pool.submit(replay_contract,str(a.data_root),task,str(a.local_output)) for task in tasks]
        for job in as_completed(futures):
            print("REPLAY_COMPLETE",*job.result(),flush=True)


if __name__=="__main__":
    main()
