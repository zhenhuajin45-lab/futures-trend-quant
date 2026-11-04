"""Post-run event and paired opportunity diagnosis on the registered historical view."""
import argparse
from collections import Counter, defaultdict
from bisect import bisect_left
from datetime import datetime, timedelta
import gzip
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read, write, stamp, digest
from backtest.reference_events import atr_directional_change_reference, score_confirmed_turn_events
from backtest.leg_scoring import score_reference_leg_events
from futures_trend_quant.data.gm_bars import bars_from_records
from futures_trend_quant.core.risk import build_stop_plan
from tools.historical_strategy.price_paths import label_opportunity, session_of, ExpectedClock
from tools.historical_strategy.inventory import contract_quality


def distribution(values):
    data=[float(x) for x in values if x is not None and np.isfinite(x)]
    if not data:
        return dict(n=0)
    return dict(n=len(data),mean=float(np.mean(data)),q25=float(np.quantile(data,.25)),
                median=float(np.median(data)),q75=float(np.quantile(data,.75)))


def public_reproduction():
    refs=read(ROOT/"reports/sprint2_p0b_reference_comparison.json")["per_product"]
    pairs={e["event_id"]:e for e in read(ROOT/"reports/sprint2_exit_rule_event_pairs.json")["events"]}
    counts=Counter()
    for product in refs.values():
        rows=sorted(product["independent_turn_events"]["events"],key=lambda x:x["confirmation_index"])
        for event,nxt in zip(rows,rows[1:]):
            if not event["observation_complete"] or nxt["confirmation_index"]>event["observation_end_index"]:
                continue
            pair=pairs[event["event_id"]]
            counts["legs"]+=1
            counts["extreme_at_confirmation"]+=nxt["pivot_index"]==event["confirmation_index"]
            for g in ("S0","A","EMA34_CONTROL"):
                second=pair["groups"][g]["second_confirmation_index"]
                counts[g+"_confirmed"]+=second is not None
                counts[g+"_before_extreme"]+=second is not None and second<nxt["pivot_index"]
    assert counts["legs"]==613 and counts["S0_before_extreme"]==282 and counts["A_before_extreme"]==292
    return dict(counts)


def interval(values,days,length):
    arr=np.array([values.get(d,[0.,0]) for d in days])
    if len(days)<length or arr[:,1].sum()==0:
        return None
    rng=np.random.default_rng(20261104)
    starts=rng.integers(0,len(days)-length+1,size=(10000,(len(days)+length-1)//length))
    indexes=(starts[:,:,None]+np.arange(length)).reshape(10000,-1)[:,:len(days)]
    totals=arr[indexes].sum(axis=1)
    valid=totals[:,1]>0
    ratios=totals[valid,0]/totals[valid,1]
    return dict(block_days=length,replicates=int(valid.sum()),low=float(np.quantile(ratios,.025)),
                high=float(np.quantile(ratios,.975)),median=float(np.median(ratios)))


def space(bars,atr,i,stop,direction):
    if i is None or stop<=i:
        return None
    future=bars[i+1:stop+1]
    if not future:
        return None
    anchor=bars[i].close
    sign=1 if direction=="bull" else -1
    return dict(forward_atr=max(0.,max(sign*((b.high if sign==1 else b.low)-anchor) for b in future))/atr[i],
                adverse_atr=max(0.,max(sign*(anchor-(b.low if sign==1 else b.high)) for b in future))/atr[i],
                close_forward_atr=max(0.,max(sign*(b.close-anchor) for b in future))/atr[i],
                n=len(future))


def event_rows(task,traces,expected):
    bars=bars_from_records([r["bar"] for r in traces])
    atr=[r["groups"]["S0"]["atr14"] for r in traces]
    reference=atr_directional_change_reference(bars,atr)
    rows=[]
    events=list(reference.events)
    ordinal={t:i for i,t in enumerate(pd.DatetimeIndex(expected).ceil("15min").unique().sort_values())}
    def checked_space(i,end,direction):
        if i is None or end>=len(traces) or end<=i:
            return None
        start_time=stamp(bars[i].eob)
        end_time=stamp(bars[end].eob)
        if (ordinal.get(end_time,-100000)-ordinal.get(start_time,100000)!=end-i or
                not all(r["complete_m15"] for r in traces[i+1:end+1])):
            return None
        return space(bars,atr,i,end,direction)
    for group in ("S0","A","EMA34_CONTROL"):
        pred=[r["groups"][group]["trend_decision"]["state"]["trend_state"] for r in traces]
        scored=score_confirmed_turn_events(pred,events)
        legs=score_reference_leg_events(pred,scored["events"])["events"]
        for n,event in enumerate(legs):
            c=event["confirmation_index"]
            if not traces[c]["selected"]:
                continue
            second=event["second_confirmation_index"]
            direction=event["new_direction"]
            end=event["leg_valid_end_index"]
            nxt=events[n+1] if n+1<len(events) else None
            future_complete=bool(event["observation_complete"] and checked_space(c,c+48,direction) is not None)
            fast=bool(future_complete and nxt and nxt.confirmation_index<=event["observation_end_index"])
            extreme=nxt.pivot_index if fast else None
            first_candidate=next((j for j in range(c,end+1) if
                (traces[j]["groups"][group].get("entry_evaluation") or {}).get("accepted")),None)
            first_lost=next((j for j in range(second+1,end+1) if pred[j]!=direction),None) if second is not None else None
            rows.append(dict(csymbol=task["csymbol"],actual_contract=task["symbol"],group=group,
                event_id=event["event_id"],direction=direction,pivot_eob=event["pivot_eob"],
                confirmation_eob=event["confirmation_eob"],first_pair=None if second is None else second-1,
                first_direction_index=event["first_target_index"],
                first_pair_eob=None if second is None else bars[second-1].eob.isoformat(),
                second_pair=second,actionable_eob=None if second is None else bars[second].eob.isoformat(),
                first_lost_direction_eob=None if first_lost is None else bars[first_lost].eob.isoformat(),
                old_first_pair_delay=None if second is None else second-1-c,
                leg_end_eob=bars[end].eob.isoformat(),already_authorized_at_reference=pred[c]==direction,
                fast_complete_leg=fast,before_close_extreme=second is not None and extreme is not None and second<extreme,
                missing_pair=second is None,censored=not future_complete,
                first_candidate_eob=None if first_candidate is None else bars[first_candidate].eob.isoformat(),
                actionable_to_candidate_bars=None if second is None or first_candidate is None else first_candidate-second,
                first_candidate_remaining_leg_space=checked_space(first_candidate,end,direction),
                remaining_bars=None if second is None else end-second,
                already_moved_atr=abs(bars[c].close-event["pivot_price"])/atr[c],
                actionable_moved_atr=None if second is None else abs(bars[second].close-event["pivot_price"])/atr[second],
                reference_space8=checked_space(c,c+8,direction),
                reference_space48=checked_space(c,c+48,direction),
                actionable_space8=checked_space(second,second+8,direction) if second is not None else None,
                actionable_space48=checked_space(second,second+48,direction) if second is not None else None,
                remaining_leg_space=checked_space(second,end,direction)))
    return rows


def extract(task,traces,tick,rules):
    opportunities,controls=[],[]
    flow=Counter()
    seen={}
    rejected={}
    last_state=None
    age=episode=0
    for i,row in enumerate(traces):
        core=row["groups"]["S0"]
        state=core["trend_decision"]["state"]["trend_state"]
        age=age+1 if state==last_state else 1
        episode+=state!=last_state
        last_state=state
        if not row["selected"]:
            continue
        flow["selected_complete_m15"]+=1
        if state=="none":
            flow["none"]+=1
            continue
        flow["authorized"]+=1
        entry=core["entry_evaluation"]
        branch="gate_open" if entry["gate_open"] else "gate_closed"
        flow[branch]+=1
        if len(entry["formed_paths"])>1:
            flow[branch+"_overlap_"+"+".join(entry["formed_paths"])]+=1
        for path in entry["formed_paths"]:
            flow[branch+"_formed_"+path]+=1
        signal=dict(csymbol=task["csymbol"],actual_contract=task["symbol"],time=stamp(row["decision_eob"]),
                    direction=state,atr=float(core["atr14"]),close=float(core["close"]),i=i,
                    trading_date=row["trading_date"],session=session_of(stamp(row["bar"]["bob"])),
                    gate=entry["gate_open"],age=age,episode=episode)
        if not entry["accepted"]:
            flow["blocked_"+entry["reason"]]+=1
            controls.append(signal)
            continue
        if not entry.get("evidence_id"):
            flow["accepted_without_identifiable_evidence"]+=1
            continue
        flow["accepted_signal_rows"]+=1
        flow["selected_path_"+entry["path"]]+=1
        key=(task["csymbol"],task["symbol"],state,entry["path"],entry["evidence_id"])
        try:
            build_stop_plan(direction=state,entry=signal["close"],anchor=entry["anchor"]["anchor_price"],
                            atr=signal["atr"],tick=tick,buffer_atr=rules["stop_buffer_atr"],
                            min_atr=rules["stop_min_atr"],max_atr=rules["stop_max_atr"],reward_r=2)
        except ValueError as exc:
            flow["risk_rejected_signal_rows"]+=1
            rejected.setdefault(key,dict(opportunity_id="|".join(key),time=signal["time"],reason=str(exc)))
            continue
        flow["valid_structural_stop_rows"]+=1
        if key in seen:
            seen[key]["repeat_rows"]+=1
            continue
        signal.update(path=entry["path"],evidence_id=entry["evidence_id"],trigger_bar_id=entry["trigger_bar_id"],
                      formed_paths=entry["formed_paths"],anchor=entry["anchor"]["anchor_price"],
                      repeat_rows=0,opportunity_id="|".join(key))
        seen[key]=signal
        opportunities.append(signal)
    flow["unique_first_valid_structural_evidence"]=len(opportunities)
    flow["repeat_valid_evidence_rows"]=sum(o["repeat_rows"] for o in opportunities)
    return opportunities,controls,flow,list(rejected.values())


def match_controls(opportunities,controls):
    """All decisions use own-time facts only. No path labels or future extrema exist here."""
    used=set()
    result={}
    for signal in sorted(opportunities,key=lambda r:r["time"]):
        candidates=[]
        for n,control in enumerate(controls):
            if n in used or any(signal[k]!=control[k] for k in
                ("actual_contract","direction","trading_date","session","gate")):
                continue
            ratio=(control["atr"]/control["close"])/(signal["atr"]/signal["close"])
            if not .8<=ratio<=1.2:
                continue
            priority=(abs(control["age"]-signal["age"]),abs((control["time"]-signal["time"]).total_seconds()),control["time"],n)
            candidates.append(priority)
        if candidates:
            n=min(candidates)[-1]
            used.add(n)
            result[signal["opportunity_id"]]=dict(controls[n])
    return result


def summary_labels(rows,horizon):
    labels=Counter(r["labels"][str(horizon)]["label"] for r in rows)
    complete=[r["labels"][str(horizon)] for r in rows if r["labels"][str(horizon)].get("complete")]
    n=len(complete)
    target=sum(x["label"]=="TARGET_FIRST" for x in complete)
    both=sum(x["label"]=="BOTH_SAME_M1" for x in complete)
    geometry=dict(complete_n=n,
        fixed_3atr_forward_reached=sum(x.get("mfe_r",0)*x.get("r_over_atr",0)>=3 for x in complete),
        fixed_3atr_reached_but_2r_unreached=sum(x.get("mfe_r",0)*x.get("r_over_atr",0)>=3 and x.get("mfe_r",0)<2 for x in complete),
        reached_2r_after_stop_first=sum(x["label"]=="STOP_FIRST" and x.get("mfe_r",0)>=2 for x in complete),
        forward_mfe_atr=distribution(x.get("mfe_r",0)*x.get("r_over_atr",0) for x in complete),
        adverse_mae_atr=distribution(x.get("mae_r",0)*x.get("r_over_atr",0) for x in complete))
    admissible=[r["labels"][str(horizon)] for r in rows if r["labels"][str(horizon)]["label"] not in ("INVALID_AT_OPEN","INVALID_PRICE_LEVEL")]
    known=sum(x["label"]=="TARGET_FIRST" for x in admissible)
    uncertain=sum(x["label"] in ("BOTH_SAME_M1","CENSORED") for x in admissible)
    return dict(unique=len(rows),labels=dict(labels),complete_denominator=n,
                target_first_lower=target/n if n else None,target_first_upper=(target+both)/n if n else None,
                all_admissible_sensitivity=dict(n=len(admissible),lower=known/len(admissible) if admissible else None,
                    upper=(known+uncertain)/len(admissible) if admissible else None),
                complete_counts=dict(Counter(x["label"] for x in complete)),
                price_space_geometry=geometry,
                mfe_r=distribution(x.get("mfe_r") for x in complete),mae_r=distribution(x.get("mae_r") for x in complete),
                r_over_atr=distribution(x.get("r_over_atr") for x in complete),
                known_touch_before_censor=sum(r["labels"][str(horizon)].get("known_touch_before_censor",False) for r in rows),
                cross_session_entries=sum(r["labels"][str(horizon)].get("cross_session",False) for r in rows),
                one_price_entry_bars=sum(bool(r["labels"][str(horizon)].get("one_price_entry")) for r in rows),
                open_first_touches=sum(r["labels"][str(horizon)].get("first_touch_point")=="OPEN" for r in rows),
                touch_after_session_break=sum(r["labels"][str(horizon)].get("first_touch_after_session_break",False) for r in rows),
                entry_gap_from_signal_atr=distribution(r["labels"][str(horizon)].get("entry_gap_from_signal_atr") for r in rows),
                complete_fraction=n/len(rows) if rows else None)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data-root",type=Path,required=True)
    p.add_argument("--replay-root",type=Path,required=True)
    p.add_argument("--local-output",type=Path,required=True)
    p.add_argument("--summary",type=Path,required=True)
    p.add_argument("--products",default="")
    a=p.parse_args()
    a.local_output.mkdir(exist_ok=True,parents=True)
    manifest=read(a.data_root/"manifest.json")
    rules=read(ROOT/"config/strategy.json")["rules"]
    ticks=read(ROOT/"config/fees.json")["products"]
    all_ops,all_controls,events,rejected_levels=[],[],[],[]
    funnels={}
    processed=[]
    for task in manifest["tasks"]:
        if a.products and task["csymbol"] not in set(a.products.split(",")):
            continue
        file=a.replay_root/(task["symbol"]+".jsonl.gz")
        if not file.exists():
            continue
        with gzip.open(file,"rt",encoding="utf8") as stream:
            header=json.loads(next(stream))
            traces=[json.loads(line) for line in stream]
        if header["input_sha256"]!=digest(a.data_root/task["path"]):
            raise ValueError("replay input identity mismatch")
        raw=pd.read_parquet(a.data_root/task["path"])
        raw_records=raw.to_dict("records")
        raw_context=dict(list=raw_records,bobs=list(raw.bob),by_eob={row["eob"]:row for row in raw_records})
        expected=[stamp(row["eob"]) for row in read(a.data_root/(task["csymbol"]+"_schedule.json"))]
        raw_context["clock_index"]=ExpectedClock(expected)
        selection=read(a.data_root/(task["csymbol"]+"_selection.json"))
        ordered=list(selection)
        quality,_=contract_quality(raw,expected)
        for row in traces:
            row["selected"]=row["selected"] and quality.get(stamp(row["bar"]["eob"]),False)
        tick=ticks[task["csymbol"].split(".")[1]]["price_tick"]
        ops,controls,flow,rejected=extract(task,traces,tick,rules)
        rejected_levels.extend(rejected)
        matched=match_controls(ops,controls)
        for op in ops:
            op["labels"]={str(h):label_opportunity(op,raw_context,expected,tick,rules,h) for h in (8,48)}
            op["month"]=op["trading_date"][:7]
            # Previous-day proxy changes only at exchange-day boundaries.
            at=ordered.index(op["trading_date"])
            near=any(selection[d]!=op["actual_contract"] for d in ordered[max(0,at-1):at+2])
            op["roll_status"]="near_selection_change" if near else "within_actual_contract"
            next_exit=next((r for r in traces[op["i"]+1:] if r["groups"]["S0"]["trend_decision"]["confirmed"]
                           and r["groups"]["S0"]["trend_decision"]["confirmed_state"]!=op["direction"]),None)
            op["trend_exit_decision"]=None if next_exit is None else next_exit["decision_eob"]
            exit_index=bisect_left(raw_context["bobs"],stamp(next_exit["decision_eob"])) if next_exit else len(raw_records)
            exit_bar=raw_records[exit_index] if exit_index<len(raw_records) else None
            op["trend_exit_reference"]=None if exit_bar is None else dict(bob=exit_bar["bob"].isoformat(),price=float(exit_bar["open"]),
                execution_proxy=True,liquidity_status="UNKNOWN")
            touch=op["labels"]["48"].get("first_touch_eob")
            touch_at=stamp(touch) if touch else None
            if touch_at is not None and op["labels"]["48"].get("first_touch_point")=="OPEN":
                touch_at-=timedelta(minutes=1)
            op["trend_exit_before_touch"]=bool(exit_bar and touch_at is not None and exit_bar["bob"]<touch_at)
            op["trend_exit_same_time_as_touch"]=bool(exit_bar and touch_at is not None and exit_bar["bob"]==touch_at)
            control=matched.get(op["opportunity_id"])
            op["matched"]=control is not None
            if control is not None:
                ratio=op["labels"]["48"].get("r_over_atr")
                control["opportunity_id"]=op["opportunity_id"]
                control["labels"]={str(h):(label_opportunity(control,raw_context,expected,tick,rules,h,risk_atr=ratio)
                                                   if ratio else dict(label="INVALID_PRICE_LEVEL",complete=False))
                                   for h in (8,48)}
                all_controls.append(control)
        flow["full_price_clock_h48"]=sum(op["labels"]["48"].get("complete",False) for op in ops)
        flow["matched_controls"]=len(matched)
        all_ops.extend(ops)
        events.extend(event_rows(task,traces,expected))
        funnels[task["symbol"]]=dict(flow)
        processed.append(task["symbol"])
        print("ANALYZED",task["symbol"],len(ops),len(matched),flush=True)
    for name,rows in (("opportunities",all_ops),("controls",all_controls),("events",events),("rejected_price_levels",rejected_levels)):
        (a.local_output/(name+".json")).write_text(json.dumps(rows,ensure_ascii=False,indent=2,default=str,allow_nan=False),encoding="utf8")
    control_by_id={r["opportunity_id"]:r for r in all_controls}
    paired=[r for r in all_ops if r["opportunity_id"] in control_by_id and
            r["labels"]["48"].get("complete") and control_by_id[r["opportunity_id"]]["labels"]["48"].get("complete")]
    pair_stats={}
    dates=sorted({row["trading_date"] for symbol in manifest["products"] for row in
                  read(a.data_root/(symbol+"_schedule.json"))})
    for optimistic in (False,True):
        panel=defaultdict(lambda:[0.,0])
        rescued=harmed=0
        signal_target=control_target=0
        for op in paired:
            control=control_by_id[op["opportunity_id"]]
            is_target=lambda r:r["labels"]["48"]["label"]=="TARGET_FIRST" or (optimistic and r["labels"]["48"]["label"]=="BOTH_SAME_M1")
            diff=int(is_target(op))-int(is_target(control))
            signal_target+=is_target(op)
            control_target+=is_target(control)
            rescued+=diff==1
            harmed+=diff==-1
            panel[op["trading_date"]][0]+=diff
            panel[op["trading_date"]][1]+=1
        pair_stats["optimistic" if optimistic else "pessimistic"]=dict(n=len(paired),rescued=rescued,harmed=harmed,
            signal_target=signal_target,control_target=control_target,
            signal_target_fraction=signal_target/len(paired) if paired else None,
            control_target_fraction=control_target/len(paired) if paired else None,
            difference=(rescued-harmed)/len(paired) if paired else None,
            intervals=[interval(panel,dates,b) for b in (5,10)])
    # Unlike a common optimistic convention, these permit opposite intraminute orders in each pair.
    worst={}
    for bound in ("lower","upper"):
        panel=defaultdict(lambda:[0.,0])
        for op in paired:
            control=control_by_id[op["opportunity_id"]]
            a_label=op["labels"]["48"]["label"]
            b_label=control["labels"]["48"]["label"]
            av=int(a_label=="TARGET_FIRST" or (bound=="upper" and a_label=="BOTH_SAME_M1"))
            bv=int(b_label=="TARGET_FIRST" or (bound=="lower" and b_label=="BOTH_SAME_M1"))
            panel[op["trading_date"]][0]+=av-bv
            panel[op["trading_date"]][1]+=1
        worst[bound]=dict(difference=sum(v[0] for v in panel.values())/len(paired) if paired else None,
                          intervals=[interval(panel,dates,b) for b in (5,10)])
    pair_stats["worst_ambiguity_bounds"]=worst
    pair_stats["stop_first_difference"]=sum(int(o["labels"]["48"]["label"]=="STOP_FIRST")-
        int(control_by_id[o["opportunity_id"]]["labels"]["48"]["label"]=="STOP_FIRST") for o in paired)/len(paired) if paired else None
    short_paired=[o for o in all_ops if o["opportunity_id"] in control_by_id and o["labels"]["8"].get("complete") and
                  control_by_id[o["opportunity_id"]]["labels"]["8"].get("complete")]
    short_panel=defaultdict(lambda:[0.,0])
    for op in short_paired:
        difference=int(op["labels"]["8"]["label"]=="TARGET_FIRST")-int(control_by_id[op["opportunity_id"]]["labels"]["8"]["label"]=="TARGET_FIRST")
        short_panel[op["trading_date"]][0]+=difference
        short_panel[op["trading_date"]][1]+=1
    short_stats=dict(n=len(short_paired),difference=sum(v[0] for v in short_panel.values())/len(short_paired) if short_paired else None,
                     intervals=[interval(short_panel,dates,b) for b in (5,10)])
    grouped={}
    for field in ("csymbol","direction","path","session","month","roll_status"):
        grouped[field]={}
        for value in sorted({r[field] for r in all_ops}):
            cohort=[r for r in all_ops if r[field]==value]
            grouped[field][value]=dict(summary=summary_labels(cohort,48),descriptive_only=len(cohort)<30)
    fast={}
    for group in ("S0","A","EMA34_CONTROL"):
        cohort=[e for e in events if e["group"]==group and e["fast_complete_leg"]]
        fast[group]=dict(n=len(cohort),before_extreme=sum(e["before_close_extreme"] for e in cohort),
                         already_authorized_at_reference=sum(e["already_authorized_at_reference"] for e in cohort),
                         already_authorized_with_later_pair=sum(e["already_authorized_at_reference"] and not e["missing_pair"] for e in cohort),
                         later_authorization_before_extreme=sum(not e["already_authorized_at_reference"] and e["before_close_extreme"] for e in cohort),
                         later_authorization_at_or_after_extreme=sum(not e["already_authorized_at_reference"] and not e["before_close_extreme"] and not e["missing_pair"] for e in cohort),
                         no_accepted_candidate_in_leg=sum(e["first_candidate_eob"] is None for e in cohort),
                         no_pair=sum(e["missing_pair"] for e in cohort),
                         at_or_after_extreme=sum(not e["before_close_extreme"] and not e["missing_pair"] for e in cohort),
                         actionable_space8_forward=distribution(e["actionable_space8"]["forward_atr"] for e in cohort if e["actionable_space8"]),
                         actionable_space48_forward=distribution(e["actionable_space48"]["forward_atr"] for e in cohort if e["actionable_space48"]),
                         remaining_leg_intrabar_forward=distribution(e["remaining_leg_space"]["forward_atr"] for e in cohort if e["remaining_leg_space"]),
                         remaining_leg_close_forward=distribution(e["remaining_leg_space"]["close_forward_atr"] for e in cohort if e["remaining_leg_space"]),
                         actionable_moved_atr=distribution(e["actionable_moved_atr"] for e in cohort),
                         actionable_to_candidate_bars=distribution(e["actionable_to_candidate_bars"] for e in cohort),
                         first_candidate_remaining_leg_intrabar=distribution(e["first_candidate_remaining_leg_space"]["forward_atr"] for e in cohort if e["first_candidate_remaining_leg_space"]),
                         remaining_bars=distribution(e["remaining_bars"] for e in cohort))
    matching=len(all_controls)/len(all_ops) if all_ops else None
    trend_exit_stats=dict(opportunities=len(all_ops),with_decision=sum(o["trend_exit_decision"] is not None for o in all_ops),
        before_touch=sum(o["trend_exit_before_touch"] for o in all_ops),
        same_clock_as_touch=sum(o["trend_exit_same_time_as_touch"] for o in all_ops),
        target_first_after_earlier_exit=sum(o["trend_exit_before_touch"] and o["labels"]["48"]["label"]=="TARGET_FIRST" for o in all_ops),
        stop_first_after_earlier_exit=sum(o["trend_exit_before_touch"] and o["labels"]["48"]["label"]=="STOP_FIRST" for o in all_ops),
        interpretation="CORE_TREND_EXIT_AND_NEXT_OBSERVABLE_OPEN_PROXY; NO_POSITION_OR_FILL_SIMULATION")
    summary=dict(study="HISTORICAL_2R_V6",data_manifest_sha256=digest(a.data_root/"manifest.json"),
        public_reproduction=public_reproduction(),processed_contracts=processed,expected_contracts=len(manifest["tasks"]),
        all_contracts_processed=len(processed)==len(manifest["tasks"]),opportunities=len(all_ops),
        invalid_price_evidence=len(rejected_levels),
        evidence_later_recovered_after_invalid_stop=sum(r["opportunity_id"] in {o["opportunity_id"] for o in all_ops} for r in rejected_levels),
        episodes=len({(o["csymbol"],o["actual_contract"],o["episode"]) for o in all_ops}),
        repeated_rows=sum(o["repeat_rows"] for o in all_ops),matching_coverage=matching,complete_matched_pairs=len(paired),
        signals={str(h):summary_labels(all_ops,h) for h in (8,48)},
        controls={str(h):summary_labels(all_controls,h) for h in (8,48)},pair_comparison=pair_stats,
        short_pair_comparison=short_stats,
        unmatched_signals=summary_labels([o for o in all_ops if not o["matched"]],48),
        unmatched_by={field:dict(Counter(o[field] for o in all_ops if not o["matched"])) for field in
                      ("csymbol","direction","path","session","month","roll_status")},
        trend_exit_comparison=trend_exit_stats,
        fast_reference_legs=fast,by=grouped,funnels=funnels,prices_are_execution_proxies=True,
        liquidity_status="UNKNOWN; one-price entry flags retained",net_performance_evaluated=False,
        opportunity_context="ZERO_POSITIONS_NO_FILLS; ACCOUNT_SIZING_AND_FILL_DEPENDENT_COOLDOWN_NOT_SIMULATED",
        source_clock_status="UNKNOWN_RESEARCH_ASSUMPTIONS",t3_status="NOT_RUN_PENDING_T1_T2_DECISION")
    write(a.summary,summary)


if __name__=="__main__":
    main()
