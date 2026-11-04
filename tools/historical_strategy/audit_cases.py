"""Independent arithmetic examples, normalized in R; original prices remain local."""
import argparse
from collections import Counter
from pathlib import Path
import sys
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,stamp


def arithmetic(rows,entry,stop,target,direction):
    sign=1 if direction=="bull" else -1
    for row in rows:
        opening=sign*(row.open-entry)
        downside=sign*((row.low if sign>0 else row.high)-entry)
        upside=sign*((row.high if sign>0 else row.low)-entry)
        risk=sign*(entry-stop)
        reward=sign*(target-entry)
        if opening<=-risk:
            return "STOP_FIRST",row
        if opening>=reward:
            return "TARGET_FIRST",row
        if downside<=-risk and upside>=reward:
            return "BOTH_SAME_M1",row
        if downside<=-risk:
            return "STOP_FIRST",row
        if upside>=reward:
            return "TARGET_FIRST",row
    return "NEITHER_BY_HORIZON",None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--data-root",type=Path,required=True)
    parser.add_argument("--analysis-root",type=Path,required=True)
    parser.add_argument("--summary",type=Path,required=True)
    args=parser.parse_args()
    ops=read(args.analysis_root/"opportunities.json")
    tasks={x["symbol"]:x for x in read(args.data_root/"manifest.json")["tasks"]}
    frames={}
    cases=[]
    selectors=[("LONG_TARGET",lambda o:o["direction"]=="bull" and o["labels"]["48"]["label"]=="TARGET_FIRST" and o["labels"]["48"].get("complete")),
        ("SHORT_TARGET",lambda o:o["direction"]=="bear" and o["labels"]["48"]["label"]=="TARGET_FIRST" and o["labels"]["48"].get("complete")),
        ("LONG_STOP",lambda o:o["direction"]=="bull" and o["labels"]["48"]["label"]=="STOP_FIRST" and o["labels"]["48"].get("complete")),
        ("SHORT_STOP",lambda o:o["direction"]=="bear" and o["labels"]["48"]["label"]=="STOP_FIRST" and o["labels"]["48"].get("complete")),
        ("BOTH_SAME_M1",lambda o:o["labels"]["48"]["label"]=="BOTH_SAME_M1" and o["labels"]["48"].get("complete")),
        ("NO_TOUCH",lambda o:o["labels"]["48"]["label"]=="NEITHER_BY_HORIZON" and o["labels"]["48"].get("complete")),
        ("ROLL_NEAR",lambda o:o["roll_status"]=="near_selection_change" and o["labels"]["48"].get("complete")),
        ("TREND_EXIT_BEFORE_TARGET",lambda o:o["trend_exit_before_touch"] and o["labels"]["48"]["label"]=="TARGET_FIRST" and o["labels"]["48"].get("complete")),
        ("REST_GAP_OPEN_TOUCH",lambda o:o["labels"]["48"].get("first_touch_after_session_break") and o["labels"]["48"].get("first_touch_point")=="OPEN" and o["labels"]["48"].get("complete")),
        ("INVALID_AT_OPEN",lambda o:o["labels"]["48"]["label"]=="INVALID_AT_OPEN"),
        ("CENSORED",lambda o:o["labels"]["48"]["label"]=="CENSORED")]
    chosen=[]
    absent=[]
    used=set()
    for category,predicate in selectors:
        match=next((o for o in ops if o["opportunity_id"] not in used and predicate(o)),None)
        if match is None:
            absent.append(category)
        else:
            chosen.append((category,match))
            used.add(match["opportunity_id"])
    clocks={}
    for category,op in chosen:
        label=op["labels"]["48"]
        symbol=op["actual_contract"]
        if symbol not in frames:
            frames[symbol]=pd.read_parquet(args.data_root/tasks[symbol]["path"])
        frame=frames[symbol]
        assert set(frame.symbol)=={symbol}
        if label["label"]=="INVALID_AT_OPEN":
            sign=1 if op["direction"]=="bull" else -1
            assert sign*(label["entry"]-label["stop"])<=0
            cases.append(dict(category=category,opportunity_id=op["opportunity_id"],label="INVALID_AT_OPEN",
                entry_eob=label["entry_eob"],stop_beyond_entry_atr=sign*(label["stop"]-label["entry"])/op["atr"],passed=True))
            continue
        # Independently derive the 48 subsequent exchange-clock M15 groups.
        if op["csymbol"] not in clocks:
            all_times=pd.DatetimeIndex([stamp(x["eob"]) for x in read(args.data_root/(op["csymbol"]+"_schedule.json"))])
            clocks[op["csymbol"]]=(all_times,all_times.ceil("15min"))
        all_times,all_buckets=clocks[op["csymbol"]]
        mask=all_times>stamp(op["time"])
        buckets=all_buckets[mask].unique()[:48]
        expected=list(all_times[mask & all_buckets.isin(buckets)])
        rows=frame.loc[frame.eob.isin(expected)].sort_values("eob")
        if label["label"]=="CENSORED":
            missing=sorted(set(expected)-set(rows.eob))
            prefix=rows.loc[rows.eob<missing[0]] if missing else rows
            if label.get("entry") is not None:
                result,_=arithmetic(list(prefix.itertuples()),label["entry"],label["stop"],label["target"],op["direction"])
                assert result=="NEITHER_BY_HORIZON"
            assert missing or len(buckets)<48 or expected[-1]!=buckets[-1]
            cases.append(dict(category=category,opportunity_id=op["opportunity_id"],label="CENSORED",
                first_missing_eob=missing[0].isoformat() if missing else None,observed_buckets=len(buckets),
                known_prefix_minutes=len(prefix),passed=True))
            continue
        assert set(rows.eob)==set(expected)
        calculated,touch=arithmetic(list(rows.itertuples()),label["entry"],label["stop"],label["target"],op["direction"])
        assert calculated==label["label"]
        if touch is not None:
            assert touch.eob==stamp(label["first_touch_eob"])
        sign=1 if op["direction"]=="bull" else -1
        normalized=None if touch is None else {k:sign*(float(getattr(touch,k))-label["entry"])/label["r"] for k in ("open","high","low","close")}
        cases.append(dict(category=category,opportunity_id=op["opportunity_id"],signal_eob=op["time"],direction=op["direction"],
            label=calculated,stop_R=-1,target_R=sign*(label["target"]-label["entry"])/label["r"],
            entry_eob=label["entry_eob"],touch_eob=label.get("first_touch_eob"),touch_ohlc_in_directional_R=normalized,
            observed_minutes=len(rows),cross_session=label["cross_session"],roll_status=op["roll_status"],
            trend_exit_decision=op["trend_exit_decision"],trend_exit_reference=op["trend_exit_reference"]["bob"] if op["trend_exit_reference"] else None,
            same_actual_contract_for_entire_path=True,passed=True))
    assert len(cases)>=8,"fewer than eight real arithmetic examples"
    write(args.summary,dict(independent_arithmetic_examples=cases,real_case_count=len(cases),
        availability_counts=dict(Counter(x["labels"]["48"]["label"] for x in ops)),absent_real_categories=absent,
        synthetic_edge_examples="tools/historical_strategy/test_price_paths.py: gap-stop, gap-target, both, missing, invalid-open"))


if __name__=="__main__":
    main()
