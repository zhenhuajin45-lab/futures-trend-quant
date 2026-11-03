"""Historical raw-price diagnostics. Never changes old cache or recovers old arrival clocks."""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backtest.forward_v3 import read, write, digest, stamp, check_lock
from futures_trend_quant.data.development_export import contract_export_windows
from futures_trend_quant.data.gm_export import export_history_to_parquet
from backtest.aggregation import aggregate_bars
from futures_trend_quant.data.gm_bars import bars_from_records

CASES = (("DCE.EG","2026-08-24","2026-08-21"),
         ("SHFE.AG","2026-07-20","2026-07-17"))


def request_window(first_open, last_close, bridge_at=None, listed_at=None):
    """Caller must supply date-effective authoritative session and bridge timestamps."""
    start = min(first_open, bridge_at) if bridge_at is not None else first_open
    if listed_at is not None:
        start = max(start, listed_at)
    if start > last_close:
        raise ValueError("listing/session bounds leave no history")
    return start, last_close


def compare_frames(old, new, start, end):
    old, new = old.copy(), new.copy()
    for frame in (old,new):
        frame["eob"] = [stamp(v) for v in frame.eob]
    old = old[old.eob.between(start,end)]
    new = new[new.eob.between(start,end)]
    before, after = set(old.eob), set(new.eob)
    added = sorted(after-before)
    common = old.merge(new,on=["symbol","eob"],suffixes=("_old","_new"))
    result=dict(old_rows=len(old),new_rows=len(new),newly_recovered_m1=len(added),
                new_only_first_eob=str(added[0]) if added else None,
                new_only_last_eob=str(added[-1]) if added else None,
                old_only_minutes=len(before-after),
                common_rows=len(common),
                common_ohlc_changed=int(sum(any(abs(row[k+"_old"]-row[k+"_new"])>1e-8
                                             for k in ("open","high","low","close"))
                                           for row in common.to_dict("records"))))
    for label,frame in (("old",old),("new",new)):
        if "bob" in frame:
            bars=bars_from_records(frame.assign(frequency="60s").to_dict("records"))
            result[label+"_observed_m15"]=len(aggregate_bars(bars,15,"900s"))
    return result


def scan_rolls(root, audit, manifest):
    rows=[]
    for product, info in audit["products"].items():
        mapping=pd.read_parquet(root/manifest["products"][product]["mapping_path"])
        windows={w.symbol:w for w in contract_export_windows(dict(zip(pd.to_datetime(mapping.trading_date).dt.date,mapping.symbol)))}
        for roll in info["rolls"]:
            start=windows[roll["new_contract"]].start_time
            case_open=stamp(roll["known_clock_fixture"]["mapping_known_at"]) if roll.get("known_clock_fixture") else None
            rows.append(dict(csymbol=product,trading_date=roll["trading_date"],
                             monday=datetime.fromisoformat(roll["trading_date"]).weekday()==0,
                             old_contract=roll["old_contract"],new_contract=roll["new_contract"],
                             reconstructed_query_start=start.isoformat(),
                             fixture_first_open=str(case_open) if case_open is not None else None,
                             start_later_than_audit_case=case_open is not None and stamp(start.isoformat()+"+08:00")>case_open,
                             prior_close_missing_legs=roll["previous_close_missing_legs"],
                             observed_single_leg_minutes=roll["roll_day_single_leg_minutes"],
                             authoritative_historical_session="PENDING",
                             exact_old_execution_request="EXPORT_HISTORY_LOG_NOT_LOCATED"))
    return rows


def minute_diagnostics(edb_root, output):
    results=[]
    for symbol,day in (("CZCE.CF701","2026-08-14"),("CZCE.CJ701","2026-08-11"),
                       ("DCE.eg2609","2026-08-24"),("DCE.eg2610","2026-08-24")):
        path=edb_root/(symbol+"_"+day+"_diff.csv")
        df=pd.read_csv(path)
        df["eob"]=pd.to_datetime(df.eob,utc=True).dt.tz_convert("Asia/Shanghai")
        df=df[df.open_gm.notna() & df.open_edb.notna()].sort_values("eob").copy()
        def session(t):
            if t.hour>=21 or t.hour<8:
                return "night"
            if (t.hour,t.minute)<=(10,15):
                return "day_am_1"
            return "day_am_2" if (t.hour,t.minute)<=(11,30) else "day_pm"
        df["session"]=[session(t) for t in df.eob]
        segments=[]
        for name,part in df.groupby("session",sort=False):
            d=part.volume_diff
            pair=(d+d.shift(-1)).abs().le(1e-8)&d.ne(0)
            segments.append(dict(session=name,common_m1=len(part),volume_mismatch_m1=int(d.ne(0).sum()),
                                 final_cumulative_volume_diff=float(d.sum()),
                                 max_abs_cumulative_volume_diff=float(d.cumsum().abs().max()),
                                 adjacent_opposite_exact_cancel_pairs=int(pair.sum()),
                                 oi_mismatch_m1=int(part.end_oi_diff.ne(0).sum())))
        result=dict(symbol=symbol,trading_date=day,diff_sha256=digest(path),segments=segments,
                    interpretation="descriptive only; minute allocation and OI update timing not independently confirmed")
        if symbol=="DCE.eg2609":
            large=df[df.high_diff_ticks.abs().eq(8)]
            result["high_8tick_eobs"]=[str(t) for t in large.eob]
            for i,index in enumerate(large.index):
                where=df.index.get_loc(index)
                adjacent=df.iloc[max(0,where-2):where+3]
                adjacent.to_csv(output/(symbol+"_high8tick_neighbors_"+str(i)+".csv"),index=False)
                boundary=df.loc[index,"eob"].ceil("15min")
                bucket=df[df.eob.dt.ceil("15min").eq(boundary)]
                result.setdefault("affected_m15",[]).append(dict(
                    eob=str(boundary),common_m1=len(bucket),
                    high_diff_ticks=float(bucket.high_gm.max()-bucket.high_edb.max())))
        results.append(result)
    return results


def log_evidence(project):
    logs=list(project.rglob("*.log"))
    hits=[]
    unreadable=0
    for path in logs:
        try:
            text=path.read_text(encoding="utf8",errors="replace")
            count=text.count("EXPORT_HISTORY")
            if count:
                hits.append(dict(file_name=path.name,sha256=digest(path),export_history_occurrences=count))
        except OSError:
            unreadable+=1
    return dict(scope="project recursive *.log",files_found=len(logs),unreadable=unreadable,hits=hits,
                status="NOT_LOCATED_IN_SCANNED_LOGS" if not hits else "MATCHES_REQUIRE_REQUEST_REVIEW")


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gm-root",type=Path,required=True)
    p.add_argument("--local-output",type=Path,required=True)
    p.add_argument("--report",type=Path,required=True)
    p.add_argument("--edb-root",type=Path)
    p.add_argument("--diagnostics-only",action="store_true")
    args=p.parse_args()
    check_lock()
    if args.diagnostics_only:
        if not args.edb_root or not args.report.exists() or not args.local_output.exists():
            p.error("diagnostics-only requires the completed report, local output and EDB root")
        report=read(args.report)
        report["minute_diagnostics"]=minute_diagnostics(args.edb_root,args.local_output)
        report["old_export_log_search"]=log_evidence(args.gm_root.parents[2])
        for item in report["cases"]:
            symbol=item["symbol"]
            previous="2026-08-21" if item["csymbol"]=="DCE.EG" else "2026-07-17"
            old=args.gm_root/"raw"/(symbol.replace(".","_")+".parquet")
            new=args.local_output/(symbol.replace(".","_")+".parquet")
            item.update(compare_frames(pd.read_parquet(old),pd.read_parquet(new),
                                       stamp(previous+"T21:01:00+08:00"),stamp(item["trading_date"]+"T15:00:00+08:00")))
        write(args.report,report)
        return
    if args.local_output.exists():
        raise ValueError("use a fresh output directory; no --resume is supported")
    args.local_output.mkdir(parents=True)
    audit=read(ROOT/"reports/v3_asof_roll_audit.json")
    manifest=read(args.gm_root/"manifest.json")
    identity=digest(args.gm_root/"manifest.json")
    result=dict(version="V5_HISTORICAL_PRICE_DIAGNOSTIC",old_dataset_sha256=identity,
                clock_recovery=False,old_export_log_status="NOT_LOCATED_IN_PROJECT",
                roll_scan=scan_rolls(args.gm_root,audit,manifest),cases=[],official_daily=[])
    write(args.report,result)
    from gm.api import set_token, history, get_trading_dates
    set_token(os.environ["GM_TOKEN"])
    for product,day,previous in CASES:
        roll=next(r for r in audit["products"][product]["rolls"] if r["trading_date"]==day)
        for role in ("old_contract","new_contract"):
            symbol=roll[role]
            start,end=request_window(datetime.fromisoformat(previous+"T21:00:00"),
                                     datetime.fromisoformat(day+"T15:01:00"),
                                     bridge_at=datetime.fromisoformat(previous+"T09:00:00"))
            path=args.local_output/(symbol.replace(".","_")+".parquet")
            source=args.gm_root/"raw"/(symbol.replace(".","_")+".parquet")
            old_hash=digest(source)
            item=dict(csymbol=product,symbol=symbol,trading_date=day,role=role,old_sha256=old_hash,
                      request_start=start.isoformat(),request_end=end.isoformat(),
                      previous_trading_day_check=get_trading_dates(product.split(".")[0],previous,day),
                      historical_session_authority="PENDING_HISTORICAL_RULE_ATTESTATION",
                      query_purpose="expanded diagnostic including previous day session and common close",
                      calls=[])
            def fetch(**kwargs):
                item["calls"].append({k:str(v) for k,v in kwargs.items() if k in
                                      ("symbol","frequency","start_time","end_time","skip_suspended","fill_missing","adjust")})
                return history(**kwargs)
            try:
                exported=export_history_to_parquet(symbol=symbol,start_time=start,end_time=end,
                                                   output_path=path,history_fetcher=fetch,chunk_days=3)
                item["new_sha256"]=digest(path)
                item["new_export_total_rows"]=exported.rows
                first=stamp(previous+"T21:01:00+08:00")
                last=stamp(day+"T15:00:00+08:00")
                item.update(compare_frames(pd.read_parquet(source),pd.read_parquet(path),first,last))
                item["finding"]=("EXPORT_RANGE_OMISSION_SUPPORTED" if item["newly_recovered_m1"] else "NO_NEW_PRICE_ROWS")
                item["attribution_limit"]="old execution log absent; code bounds and expanded query support mechanism"
            except Exception as exc:
                item.update(finding="DATA_ACCESS_OR_UPSTREAM_UNRESOLVED",error_type=type(exc).__name__)
            if digest(source)!=old_hash:
                raise ValueError("old input changed during diagnosis")
            result["cases"].append(item)
            write(args.report,result)
    # Preserve official source originals locally. Do not treat an HTML error page as daily data.
    for symbol,day in (("CZCE.CF701","2026-08-14"),("CZCE.CJ701","2026-08-11")):
        url="https://www.czce.com.cn/cn/DFSStaticFiles/Future/"+day[:4]+"/"+day.replace("-","")+"/FutureDataDaily.htm"
        item=dict(symbol=symbol,trading_date=day,url=url,status="PENDING_OFFICIAL")
        try:
            with urlopen(url,timeout=20) as response:
                body=response.read()
            file=args.local_output/(symbol+"_"+day+"_official.html")
            file.write_bytes(body)
            item.update(response_sha256=digest(file),bytes=len(body),
                        status="ORIGINAL_DOWNLOADED_REQUIRES_CONTENT_VERIFICATION")
        except (HTTPError,URLError,TimeoutError,OSError) as exc:
            item["error_type"]=type(exc).__name__
        result["official_daily"].append(item)
    result["official_daily"].append(dict(symbols=["DCE.eg2609","DCE.eg2610"],trading_date="2026-08-24",
        status="PENDING_OFFICIAL",url="https://www.dce.com.cn/",request_needed="date-effective official daily export; no verified machine export endpoint"))
    result["old_manifest_unchanged"]=digest(args.gm_root/"manifest.json")==identity
    result["new_dataset_id"]=__import__("hashlib").sha256(json.dumps([c.get("new_sha256") for c in result["cases"]]).encode()).hexdigest()
    write(args.report,result)


if __name__=="__main__":
    main()
