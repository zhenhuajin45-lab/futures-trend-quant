"""Replace only the two recovered-night contracts in the full-pool price panel."""
import argparse
from collections import defaultdict
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write
from tools.historical_strategy.analyze import interval


def paired(ops,controls,dates):
    lookup={x["opportunity_id"]:x for x in controls}
    rows=[o for o in ops if o["opportunity_id"] in lookup and o["labels"]["48"].get("complete") and
          lookup[o["opportunity_id"]]["labels"]["48"].get("complete")]
    cases={}
    for optimistic in (False,True):
        panel=defaultdict(lambda:[0.,0])
        signal=control=0
        for row in rows:
            def positive(x):
                label=x["labels"]["48"]["label"]
                return label=="TARGET_FIRST" or (optimistic and label=="BOTH_SAME_M1")
            av,bv=int(positive(row)),int(positive(lookup[row["opportunity_id"]]))
            signal+=av
            control+=bv
            panel[row["trading_date"]][0]+=av-bv
            panel[row["trading_date"]][1]+=1
        cases["optimistic" if optimistic else "pessimistic"]=dict(n=len(rows),signal_target=signal,control_target=control,
            difference=(signal-control)/len(rows) if rows else None,intervals=[interval(panel,dates,n) for n in (5,10)])
    return dict(unique=len(ops),matching_coverage=len(lookup)/len(ops) if ops else None,comparisons=cases)


def main():
    p=argparse.ArgumentParser()
    for name in ("data-root","main-analysis","without-analysis","summary"):
        p.add_argument("--"+name,type=Path,required=True)
    a=p.parse_args()
    replaced={"DCE.eg2610","SHFE.ag2610"}
    original_ops=read(a.main_analysis/"opportunities.json")
    original_controls=read(a.main_analysis/"controls.json")
    counter_ops=read(a.without_analysis/"opportunities.json")
    counter_controls=read(a.without_analysis/"controls.json")
    assert {o["actual_contract"] for o in counter_ops}<=replaced
    combined_ops=[o for o in original_ops if o["actual_contract"] not in replaced]+counter_ops
    combined_controls=[o for o in original_controls if o["actual_contract"] not in replaced]+counter_controls
    manifest=read(a.data_root/"manifest.json")
    days=sorted({x["trading_date"] for symbol in manifest["products"] for x in read(a.data_root/(symbol+"_schedule.json"))})
    base=paired(original_ops,original_controls,days)
    counter=paired(combined_ops,combined_controls,days)
    old={o["opportunity_id"] for o in original_ops}
    new={o["opportunity_id"] for o in combined_ops}
    write(a.summary,dict(primary=base,without_450_recovered_minutes=counter,
        only_two_actual_contracts_replaced=True,unchanged_other_contracts=True,
        common_opportunity_ids=len(old&new),primary_only_ids=len(old-new),without_only_ids=len(new-old),
        point_difference_sign_unchanged={mode:(base["comparisons"][mode]["difference"]>0)==(counter["comparisons"][mode]["difference"]>0)
            for mode in ("pessimistic","optimistic")},
        original_missing_nights_are_not_a_valid_alternative_source=True,
        interpretation="DATA_ERROR_SENSITIVITY_ONLY; PRIMARY_USES_VERIFIED_RECOVERY"))


if __name__=="__main__":
    main()
