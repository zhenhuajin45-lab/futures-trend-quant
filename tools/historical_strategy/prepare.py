"""Build a separate unadjusted historical research view, with previous-day selection."""
import argparse
from bisect import bisect_left
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys

import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import check_lock, digest, read, write, stamp, expected_times
from backtest.aggregation import aggregate_bars
from futures_trend_quant.data.gm_bars import bars_from_records
from futures_trend_quant.data.gm_export import export_history_to_parquet


def merge_missing(base, patch):
    """Original rows win. Revisions are counted, never silently substituted."""
    keys=["symbol","eob"]
    common=base.merge(patch,on=keys,suffixes=("_old","_new"))
    differences={k:int((common[k+"_old"]!=common[k+"_new"]).sum())
                 for k in ("open","high","low","close","volume","position") if k in base and k in patch}
    result=pd.concat([base,patch],ignore_index=True).drop_duplicates(keys,keep="first").sort_values("eob")
    return result,dict(added=len(result)-len(base),common_revisions=differences)


def make_schedule(dates, intervals):
    schedule={}
    for previous, day in zip(dates,dates[1:]):
        segments=[]
        for part in intervals:
            h=int(part["start"].split(":")[0])
            begin=stamp((previous if h>=20 else day)+"T"+part["start"]+":00+08:00")
            finish=stamp(begin.date().isoformat()+"T"+part["end"]+":00+08:00")
            if finish<=begin:
                finish+=timedelta(days=1)
            segments.append([begin.isoformat(),finish.isoformat()])
        for t in expected_times(sorted(segments)):
            schedule[t]=day
    return schedule


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--gm-root",type=Path,required=True)
    p.add_argument("--patch-root",type=Path,required=True)
    p.add_argument("--session-probe",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--augment",action="store_true")
    a=p.parse_args()
    check_lock()
    if a.output.exists():
        raise ValueError("fresh research directory required")
    a.output.mkdir(parents=True)
    (a.output/"contracts").mkdir()
    (a.output/"augments").mkdir()
    source=read(a.gm_root/"manifest.json")
    sessions=read(a.session_probe)
    registration=read(ROOT/"tools/historical_strategy/registration.json")
    from gm.api import set_token, get_trading_dates
    import os
    set_token(os.environ["GM_TOKEN"])
    calendar={ex:get_trading_dates(ex,"2026-05-01","2026-11-03") for ex in ("CZCE","DCE","GFEX","SHFE")}
    write(a.output/"calendars.json",calendar)
    result=dict(study=registration["study"],registration_sha256=digest(ROOT/"tools/historical_strategy/registration.json"),
                original_manifest_sha256=digest(a.gm_root/"manifest.json"),products={},tasks=[],
                historical_mapping_clock="UNKNOWN; PREVIOUS_COMPLETED_DAY_PROXY",
                historical_session_proxy="2026-11-03 authorized metadata; exceptional closures unverified",
                session_source_sha256=digest(a.session_probe),supplemented_original_rows=0)
    write(a.output/"manifest.json",result)
    for csymbol,product in source["products"].items():
        exchange=csymbol.split(".")[0]
        dates=calendar[exchange]
        intervals=sessions["sessions"][csymbol][0]["time_trading"]
        schedule=make_schedule(dates,intervals)
        schedule_rows=[dict(eob=t.isoformat(),trading_date=d) for t,d in sorted(schedule.items())
                       if registration["sample_start"]<=d<=registration["sample_end"]]
        write(a.output/(csymbol+"_schedule.json"),schedule_rows)
        mapping_frame=pd.read_parquet(a.gm_root/product["mapping_path"])
        mapping={str(row.trading_date):row.symbol for row in mapping_frame.itertuples()}
        selection={day:mapping.get(dates[i-1]) for i,day in enumerate(dates) if i and
                   registration["sample_start"]<=day<=registration["sample_end"]}
        write(a.output/(csymbol+"_selection.json"),selection)
        product_record=dict(mapping_sha256=digest(a.gm_root/product["mapping_path"]),contracts=[],
                            previous_day_mapping_unavailable=sum(s is None for s in selection.values()))
        result["products"][csymbol]=product_record
        for file in product["raw_files"]:
            path=a.gm_root/file["path"]
            original_hash=digest(path)
            base=pd.read_parquet(path)
            symbol=str(base.symbol.iloc[0])
            old_rows=len(base)
            item=dict(symbol=symbol,original_sha256=original_hash,original_rows=old_rows,patches=[],augmentation=None)
            patch=a.patch_root/path.name
            if patch.exists():
                base,changes=merge_missing(base,pd.read_parquet(patch))
                item["patches"].append(dict(sha256=digest(patch),**changes))
                result["supplemented_original_rows"]+=changes["added"]
            if a.augment:
                # The currently permitted rolling boundary may have advanced by a day.
                # Keep cached earlier rows, and query only missing prehistory in a new file.
                start=datetime(2026,5,9)
                earliest=pd.Timestamp(base.bob.min()).tz_localize(None).to_pydatetime()
                if earliest>start+timedelta(days=1):
                    target=a.output/"augments"/path.name
                    try:
                        export_history_to_parquet(symbol=symbol,start_time=start,end_time=earliest,
                                                  output_path=target,chunk_days=20)
                        base,changes=merge_missing(base,pd.read_parquet(target))
                        item["augmentation"]=dict(sha256=digest(target),**changes)
                    except Exception as exc:
                        item["augmentation"]=dict(status="UNAVAILABLE",error_type=type(exc).__name__)
            base["eob"]=[stamp(t) for t in base.eob]
            base["bob"]=[stamp(t) for t in base.bob]
            keep=base.eob.isin(schedule)
            item["outside_proxy_session_rows"]=int((~keep).sum())
            base=base.loc[keep].sort_values("eob").copy()
            base["trading_date"]=[schedule[t] for t in base.eob]
            base["frequency"]="60s"
            bars=aggregate_bars(bars_from_records(base.to_dict("records")),15,"900s")
            eligible=[b for i,b in enumerate(bars) if i>=1199 and selection.get(schedule[b.eob])==symbol]
            item.update(rows=len(base),closed_m15=len(bars),selected_after_warmup_m15=len(eligible),
                        first_eligible_eob=eligible[0].eob.isoformat() if eligible else None,
                        last_eligible_eob=eligible[-1].eob.isoformat() if eligible else None)
            out=a.output/"contracts"/path.name
            base.to_parquet(out,index=False)
            item["research_sha256"]=digest(out)
            item["path"]=out.relative_to(a.output).as_posix()
            if eligible:
                result["tasks"].append(dict(csymbol=csymbol,symbol=symbol,path=item["path"],
                                           selected_m15=len(eligible),rows=len(base)))
            if digest(path)!=original_hash:
                raise ValueError("original contract changed")
            product_record["contracts"].append(item)
            write(a.output/"manifest.json",result)
            print("CONTRACT_VIEW",csymbol,symbol,len(base),len(eligible),flush=True)
        product_record["selected_m15"]=sum(c["selected_after_warmup_m15"] for c in product_record["contracts"])
    if digest(a.gm_root/"manifest.json")!=result["original_manifest_sha256"]:
        raise ValueError("original manifest changed")
    result["complete"]=True
    write(a.output/"manifest.json",result)


if __name__=="__main__":
    main()
