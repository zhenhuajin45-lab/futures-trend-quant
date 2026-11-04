"""Reproduce the task's two archived evidence examples; no market API needed."""
from pathlib import Path
from statistics import median
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write
from tools.historical_strategy.analyze import public_reproduction


def main():
    prior=public_reproduction()
    rows=read(ROOT/"reports/sprint2_exit_rule_added_exits.json")["groups"]["A"]
    flips=[r for r in rows if r["window_complete"] and r["confirmed_state"]!="none" and r["confirmed_state"]!=r["prior_state"]]
    signals=[r for r in flips if (r.get("entry_evaluation") or {}).get("accepted")]
    values=[max(0,r["old_direction_adverse_atr"]) for r in signals]
    assert len(rows)==119 and len(flips)==114 and len(signals)==30
    assert sum(x>=3 for x in values)==3 and abs(median(values)-.96)<.01
    write(ROOT/"reports/v6_public_baseline_reproduction.json",dict(fast_legs=prior,
        old_h1_selected_sample=dict(added_exits=len(rows),complete_direct_flips=len(flips),technical_candidates=len(signals),
            forward8_mfe_atr_median=median(values),at_least_1atr=sum(x>=1 for x in values),at_least_3atr=sum(x>=3 for x in values)),
        interpretation="ARCHIVED_SELECTED_EXAMPLES; NOT_REPAIRED_S0_PRICE_EVENT_SAMPLE"))
    print("ARCHIVED_EVIDENCE_REPRODUCTION_PASS",len(signals),median(values),flush=True)


if __name__=="__main__":
    main()
