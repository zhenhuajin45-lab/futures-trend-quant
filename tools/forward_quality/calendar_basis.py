"""Derive the fixed first day from an independent authorized GM metadata snapshot."""
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from backtest.forward_v3 import check_lock, calendar_record, digest, expected_times, read, stamp, write


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--gm-probe",type=Path,required=True)
    p.add_argument("--local-output",type=Path,required=True)
    p.add_argument("--summary",type=Path,required=True)
    a=p.parse_args()
    lock=check_lock()
    probe=read(a.gm_probe)
    dates=probe["calendars"]["SHFE"]
    if any(probe["calendars"][ex]!=dates for ex in ("CZCE","DCE","GFEX")):
        raise ValueError("exchange calendars disagree")
    sessions=[]
    for previous,day in zip(dates,dates[1:]):
        products={}
        for s in lock["universe"]:
            segments=[]
            for segment in probe["sessions"][s][0]["time_trading"]:
                start,end=segment["start"],segment["end"]
                h=int(start.split(":")[0])
                date0=previous if h>=20 else day
                date1=(datetime.fromisoformat(date0)+timedelta(days=1)).date().isoformat() if int(end.split(":")[0])<h else date0
                begin=stamp(date0+"T"+start+":00+08:00")
                finish=stamp(date1+"T"+end+":00+08:00")
                segments.append([begin.isoformat(),finish.isoformat()])
            products[s]=sorted(segments)
        sessions.append(dict(date=day,previous=previous,source="GM_AUTHORIZED_METADATA_SNAPSHOT",products=products))
    records=[calendar_record(s,lock) for s in sessions]
    first=next(r for r in records if r["scoring"])
    adjacent=records[records.index(first)-1]
    a.local_output.mkdir(exist_ok=True,parents=True)
    hashes={}
    for s in sessions:
        folder=a.local_output/s["date"]
        folder.mkdir(exist_ok=True)
        write(folder/"sessions.json",s)
        hashes[s["date"]]=digest(folder/"sessions.json")
    write(a.local_output/"calendar.json",dict(authority="GM_AUTHORIZED_API",version=digest(a.gm_probe),
          sessions=sessions,session_sha256_by_day=hashes,fixed_first_day=first["date"],decision_clock_mode="NOMINAL"))
    first_session=next(s for s in sessions if s["date"]==first["date"])
    write(a.summary,dict(fixed_first_day=first["date"],first_open=first["first_open"],last_close=first["last_close"],
          freeze_time=lock["freeze_time"],adjacent_day=adjacent,source="GM_AUTHORIZED_CALENDAR_AND_SESSION_API",
          source_snapshot_sha256=digest(a.gm_probe),observed_at=probe["last_observed_at"],
          independent_of_observed_minute_bars=True,
          exchange_official_daily_reconciliation="PENDING_SEPARATE_CROSSCHECK",
          expected_m1={s:len(expected_times(v)) for s,v in first_session["products"].items()}))


if __name__=="__main__":
    main()
