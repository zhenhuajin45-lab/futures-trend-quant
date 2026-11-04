import importlib.util
from pathlib import Path
import sys

import pandas as pd
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
s=importlib.util.spec_from_file_location("paths",Path(__file__).with_name("price_paths.py"))
paths=importlib.util.module_from_spec(s)
s.loader.exec_module(paths)


@pytest.mark.parametrize(("direction","bars","label"),[
    ("bull",[(100,121,99)],"TARGET_FIRST"),
    ("bear",[(100,101,79)],"TARGET_FIRST"),
    ("bull",[(100,105,89)],"STOP_FIRST"),
    ("bear",[(100,111,95)],"STOP_FIRST"),
    ("bull",[(100,121,89)],"BOTH_SAME_M1"),
    ("bull",[(89,121,88)],"STOP_FIRST"),
    ("bull",[(121,122,89)],"TARGET_FIRST"),
    ("bull",[(100,101,99)],"NEITHER_BY_HORIZON")
])
def test_hand_calculated_minute_order(direction,bars,label):
    clock=pd.date_range("2026-08-14T09:01:00+08:00",periods=len(bars),freq="min")
    raw={t:dict(open=o,high=h,low=l) for t,(o,h,l) in zip(clock,bars)}
    stop,target=(90,120) if direction=="bull" else (110,80)
    assert paths.scan_minutes(raw,list(clock),100,stop,target,direction)["label"]==label


def test_missing_minute_retains_known_touch_and_censors_unresolved():
    times=list(pd.date_range("2026-08-14T09:01:00+08:00",periods=3,freq="min"))
    raw={times[0]:dict(open=100,high=121,low=99)}
    result=paths.scan_minutes(raw,times,100,90,120,"bull")
    assert result["label"]=="TARGET_FIRST" and not result["complete"]
    raw[times[0]]["high"]=101
    assert paths.scan_minutes(raw,times,100,90,120,"bull")["label"]=="CENSORED"
    assert paths.scan_minutes(raw,times,89,90,120,"bull")["label"]=="INVALID_AT_OPEN"


def test_indexed_clock_matches_registered_clock_across_breaks_and_tail():
    times=list(pd.date_range("2026-08-14T09:01:00+08:00",periods=75,freq="min"))
    times+=list(pd.date_range("2026-08-14T10:31:00+08:00",periods=60,freq="min"))
    times+=list(pd.date_range("2026-08-17T09:01:00+08:00",periods=75,freq="min"))
    clock=paths.ExpectedClock(times)
    for t in (times[0]-pd.Timedelta(minutes=1),times[14],times[20],times[74],times[120],times[-1]):
        for horizon in (1,8,48):
            assert clock.window(t,horizon)==paths.window_clock(times,t,horizon)


@pytest.mark.parametrize("direction",["bull","bear"])
def test_next_open_fixed_stop_and_tick_mirror(direction):
    t=pd.Timestamp("2026-08-14T09:00:00+08:00")
    times=list(pd.date_range(t+pd.Timedelta(minutes=1),periods=15,freq="min"))
    reflect=(lambda x:x) if direction=="bull" else (lambda x:200-x)
    rows=[]
    for i,end in enumerate(times):
        high=103.26 if i==0 else 100.1
        lo,hi=sorted((reflect(99.9),reflect(high)))
        rows.append(dict(bob=end-pd.Timedelta(minutes=1),eob=end,open=reflect(100.02),close=reflect(100.02),high=hi,low=lo))
    context=dict(list=rows,bobs=[r["bob"] for r in rows],by_eob={r["eob"]:r for r in rows},clock_index=paths.ExpectedClock(times))
    signal=dict(time=t,direction=direction,close=100.,anchor=reflect(99.),atr=1.)
    result=paths.label_opportunity(signal,context,times,.02,dict(stop_buffer_atr=.6,stop_min_atr=1.5,stop_max_atr=6.),horizon=1)
    assert result["label"]=="TARGET_FIRST" and result["complete"]
    assert result["r"]==pytest.approx(1.62)
    assert result["target"]==pytest.approx(reflect(103.26))
    assert result["stop"]==pytest.approx(reflect(98.4))


def test_entry_gap_over_known_stop_is_invalid_not_a_perfect_stop_fill():
    t=pd.Timestamp("2026-08-14T09:00:00+08:00")
    times=list(pd.date_range(t+pd.Timedelta(minutes=1),periods=15,freq="min"))
    rows=[dict(bob=e-pd.Timedelta(minutes=1),eob=e,open=98.28,high=98.4,low=98.,close=98.3) for e in times]
    context=dict(list=rows,bobs=[r["bob"] for r in rows],by_eob={r["eob"]:r for r in rows})
    result=paths.label_opportunity(dict(time=t,direction="bull",close=100.,anchor=98.9,atr=1.),context,times,.02,
        dict(stop_buffer_atr=.6,stop_min_atr=1.5,stop_max_atr=6.),horizon=1)
    assert result["label"]=="INVALID_AT_OPEN" and not result["complete"]
