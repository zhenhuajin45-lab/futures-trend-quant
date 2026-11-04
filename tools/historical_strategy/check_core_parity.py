"""Verify optimized research adapter against normal frozen S0 execution path."""
import argparse
from collections import deque
from datetime import date
import json
from pathlib import Path
import sys

import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.aggregation import SequentialAggregator
from backtest.exit_experiment_strategy import ExitExperimentFactsStrategy
from backtest.runner import StepInput
from backtest.forward_v3 import read,write,digest
from futures_trend_quant.data.gm_bars import bars_from_records
from futures_trend_quant.core.facts.pipeline import analyze_market
from futures_trend_quant.core.facts_state import FactsStateBook,update_facts_state,latest_valid_divergence


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--contract",type=Path,required=True)
    parser.add_argument("--limit-m15",type=int,default=380)
    parser.add_argument("--report",type=Path,required=True)
    args=parser.parse_args()
    rules=read(ROOT/"config/strategy.json")["rules"]
    frame=pd.read_parquet(args.contract)
    symbol=frame.symbol.iloc[0]
    normal=ExitExperimentFactsStrategy("S0",rules,{symbol:symbol})
    fast=ExitExperimentFactsStrategy("S0",rules,{symbol:symbol})
    state=FactsStateBook().for_csymbol(symbol)
    m1,m5,m15=deque(maxlen=1200),deque(maxlen=1200),deque(maxlen=1200)
    a5,a15=SequentialAggregator(5,"300s"),SequentialAggregator(15,"900s")
    checked=0
    accepted=0
    for bar in bars_from_records(frame.to_dict("records")):
        m1.append(bar)
        b5,b15=a5.update(bar),a15.update(bar)
        if b5 is not None:
            m5.append(b5)
        if b15 is None:
            continue
        m15.append(b15)
        event=StepInput(symbol,b15.eob.date(),b15,tuple(m5),tuple(m15),None,tuple(m1),())
        expected=normal(event)
        try:
            update_facts_state(state,list(m15),rules)
            analysis=analyze_market(list(m15),rules)
        except ValueError as exc:
            if not any(k in str(exc).lower() for k in ("warmup","unavailable","requires")):
                raise
            assert expected.blocked_at=="FACTS_WARMUP"
            continue
        actual=fast.run_prepared(event,symbol,fast.runtime,analysis,
            latest_valid_divergence(state,list(m15),"top"),latest_valid_divergence(state,list(m15),"bottom"),{})
        keys=("bar_id","close","ema34","atr14","slope_state","h1_absolute","top_div","bottom_div",
              "trend_inputs","trend_decision","entry_evaluation")
        for key in keys:
            if expected.facts[key]!=actual.facts[key]:
                raise AssertionError(json.dumps(dict(eob=b15.eob.isoformat(),field=key,
                    normal=expected.facts[key],prepared=actual.facts[key]),default=str))
        checked+=1
        accepted+=bool((actual.facts["entry_evaluation"] or {}).get("accepted"))
        if len(m15)>=args.limit_m15:
            break
    if checked<30:
        raise AssertionError("insufficient scored parity bars")
    write(args.report,dict(pass_all=True,contract_sha256=digest(args.contract),checked_m15=checked,
                          accepted_rows=accepted,normal_path="ExitExperimentFactsStrategy.__call__ -> run_core_step"))
    print("PARITY_PASS",checked,accepted,flush=True)


if __name__=="__main__":
    main()
