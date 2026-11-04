"""M1 price-path labels. No account, fills, fee bypass or profitability fields."""
from bisect import bisect_left, bisect_right
import math
import pandas as pd

from futures_trend_quant.core.risk import build_stop_plan, _round_up, _round_down


def session_of(time):
    hour=time.hour
    if hour<9 or hour>=20:
        return "night"
    if (hour,time.minute)<(10,15):
        return "day_am_1"
    return "day_am_2" if hour<12 else "day_pm"


def window_clock(expected, signal_time, horizon):
    following=expected[bisect_right(expected,signal_time):]
    buckets=[]
    chosen=[]
    for time in following:
        bucket=time.ceil("15min")
        if not buckets or bucket!=buckets[-1]:
            if len(buckets)==horizon:
                break
            buckets.append(bucket)
        chosen.append(time)
    complete=bool(chosen) and len(buckets)==horizon and chosen[-1]==buckets[-1]
    return chosen,complete


class ExpectedClock:
    """Index the unchanged registered M15 observation clock once per contract."""
    def __init__(self,expected):
        self.minutes=expected
        boundaries=pd.DatetimeIndex(expected).ceil("15min")
        self.ends=[i for i in range(len(expected)) if i+1==len(expected) or boundaries[i+1]!=boundaries[i]]
        self.boundaries=[boundaries[i] for i in self.ends]

    def window(self,signal_time,horizon):
        start=bisect_right(self.minutes,signal_time)
        if start>=len(self.minutes):
            return [],False
        first_bucket=bisect_left(self.ends,start)
        last_bucket=min(first_bucket+horizon-1,len(self.ends)-1)
        end=self.ends[last_bucket]
        return self.minutes[start:end+1],bool(last_bucket-first_bucket+1==horizon and self.minutes[end]==self.boundaries[last_bucket])


def scan_minutes(rows, expected, entry, stop, target, direction):
    """Expected slots are authoritative research-proxy clock. Open precedes range."""
    sign=1 if direction=="bull" else -1
    risk=sign*(entry-stop)
    if risk<=0:
        return dict(label="INVALID_AT_OPEN",complete=False,mfe_r=None,mae_r=None)
    label=None
    first_touch=None
    point=None
    forward=adverse=0.
    complete=True
    previous_time=None
    touch_after_break=False
    for time in expected:
        bar=rows.get(time)
        if bar is None:
            complete=False
            break
        forward=max(forward,sign*((bar["high"] if sign==1 else bar["low"])-entry))
        adverse=max(adverse,sign*(entry-(bar["low"] if sign==1 else bar["high"])))
        if label is not None:
            previous_time=time
            continue
        open_stop=sign*(bar["open"]-stop)<=0
        open_target=sign*(bar["open"]-target)>=0
        hit_stop=bar["low"]<=stop if sign==1 else bar["high"]>=stop
        hit_target=bar["high"]>=target if sign==1 else bar["low"]<=target
        if open_stop or open_target:
            label="STOP_FIRST" if open_stop else "TARGET_FIRST"
            point="OPEN"
        elif hit_stop and hit_target:
            label="BOTH_SAME_M1"
            point="INTRAMINUTE"
        elif hit_stop or hit_target:
            label="STOP_FIRST" if hit_stop else "TARGET_FIRST"
            point="INTRAMINUTE"
        if label is not None:
            first_touch=time
            touch_after_break=previous_time is not None and (time-previous_time).total_seconds()>60
        previous_time=time
    return dict(label=label or ("NEITHER_BY_HORIZON" if complete else "CENSORED"),complete=complete,
                first_touch_eob=None if first_touch is None else first_touch.isoformat(),first_touch_point=point,
                first_touch_after_session_break=touch_after_break,
                mfe_r=max(0.,forward)/risk,mae_r=max(0.,adverse)/risk)


def label_opportunity(signal, raw_records, expected, tick, rules, horizon=48, risk_atr=None):
    clock,full=(raw_records["clock_index"].window(signal["time"],horizon) if "clock_index" in raw_records else
                window_clock(expected,signal["time"],horizon))
    common=dict(horizon_m15=horizon,complete=False,entry_eob=None,entry_bob=None,r_over_atr=None,
                entry_gap=False,cross_session=False,one_price_entry=None,liquidity_verified=False,
                price_limit_status="UNKNOWN",entry_gap_from_signal_atr=None)
    if not clock:
        return dict(common,label="CENSORED",reason="TAIL_NO_NEXT_CLOCK")
    index=bisect_left(raw_records["bobs"],signal["time"])
    if index>=len(raw_records["list"]):
        return dict(common,label="CENSORED",reason="NO_NEXT_ACTUAL_CONTRACT_MINUTE")
    bar=raw_records["list"][index]
    common.update(entry_eob=bar["eob"].isoformat(),entry_bob=bar["bob"].isoformat(),
                  entry_gap=bar["eob"]!=clock[0],cross_session=bar["bob"]>signal["time"],
                  one_price_entry=bar["open"]==bar["high"]==bar["low"]==bar["close"])
    if common["entry_gap"]:
        return dict(common,label="CENSORED",reason="MISSING_ENTRY_MINUTE")
    entry=float(bar["open"])
    common["entry_gap_from_signal_atr"]=(entry-signal["close"])/signal["atr"]
    direction=signal["direction"]
    if risk_atr is None:
        try:
            plan=build_stop_plan(direction=direction,entry=signal["close"],anchor=signal["anchor"],
                                 atr=signal["atr"],tick=tick,buffer_atr=rules["stop_buffer_atr"],
                                 min_atr=rules["stop_min_atr"],max_atr=rules["stop_max_atr"],reward_r=2)
        except ValueError as exc:
            return dict(common,label="INVALID_PRICE_LEVEL",reason=str(exc))
        stop=plan.stop
    else:
        distance=risk_atr*signal["atr"]
        stop=_round_down(entry-distance,tick) if direction=="bull" else _round_up(entry+distance,tick)
    risk=(entry-stop) if direction=="bull" else (stop-entry)
    if risk<=0:
        return dict(common,label="INVALID_AT_OPEN",reason="ENTRY_OPEN_THROUGH_FIXED_STOP",entry=entry,stop=stop)
    target=(_round_up(entry+2*risk,tick) if direction=="bull" else _round_down(entry-2*risk,tick))
    result=scan_minutes(raw_records["by_eob"],clock,entry,stop,target,direction)
    result["complete"]=result["complete"] and full
    if not full and result["label"]=="NEITHER_BY_HORIZON":
        result["label"]="CENSORED"
    return dict(common,**result,entry=entry,stop=stop,target=target,r=risk,r_over_atr=risk/signal["atr"],
                known_touch_before_censor=not result["complete"] and result["label"] in
                ("TARGET_FIRST","STOP_FIRST","BOTH_SAME_M1"))
