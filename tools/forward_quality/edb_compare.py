"""Download four actual-contract cases from EDB and compare unadjusted GM bars."""
import argparse
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backtest.forward_v3 import digest, read, write, stamp
from backtest.aggregation import aggregate_bars
from futures_trend_quant.data.gm_bars import bars_from_records

CASES = (("CZCE.CF701","2026-08-14",5),("CZCE.CJ701","2026-08-11",5),
         ("DCE.eg2609","2026-08-24",1),("DCE.eg2610","2026-08-24",1))
ENDPOINT = "https://edb.shinnytech.com/md/kline"


def normalize_edb(frame):
    if "datetime_nano" not in frame:
        raise ValueError("EDB response lacks datetime_nano")
    frame = frame.copy()
    frame["bob"] = pd.to_datetime(frame.datetime_nano, unit="ns", utc=True).dt.tz_convert("Asia/Shanghai")
    frame["eob"] = frame.bob + timedelta(minutes=1)
    if frame.eob.duplicated().any():
        raise ValueError("EDB duplicate minute")
    return frame


def compare(gm, edb, tick):
    g, e = gm.set_index("eob"), edb.set_index("eob")
    merged = g.join(e, how="outer", lsuffix="_gm", rsuffix="_edb")
    merged["missing"] = ["GM" if pd.isna(r.open_gm) else "EDB" if pd.isna(r.open_edb) else ""
                         for r in merged.itertuples()]
    for field in ("open","high","low","close"):
        merged[field+"_diff_ticks"] = (merged[field+"_gm"]-merged[field+"_edb"])/tick
    merged["volume_diff"] = merged.volume_gm-merged.volume_edb
    merged["end_oi_diff"] = merged.position-merged.close_oi
    return merged.reset_index()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gm-root",type=Path,required=True)
    p.add_argument("--local-output",type=Path,required=True)
    p.add_argument("--report",type=Path,required=True)
    p.add_argument("--reuse-response",action="store_true")
    args=p.parse_args()
    args.local_output.mkdir(exist_ok=True,parents=True)
    report=dict(source="EDB_HTTP_FREE", endpoint=ENDPOINT, observed_at=datetime.now(timezone.utc).isoformat(),
                use="PRICE_CROSSCHECK_ONLY",historical_clock_qualification=False,
                tick_source="existing config/fees.json contract metadata, fee verification separate",cases=[])
    for symbol,day,tick in CASES:
        item=dict(symbol=symbol,trading_date=day,tick_size=tick)
        start=(datetime.fromisoformat(day)-timedelta(days=3)).strftime("%Y-%m-%d")+" 20:00:00"
        params=dict(period=60,symbol=symbol,start_time=start,end_time=day+" 15:00:00")
        try:
            file=args.local_output/(symbol+"_"+day+"_edb.csv")
            if args.reuse_response and file.exists():
                body=file.read_bytes()
                item["response_reused"]=True
            else:
                with urlopen(ENDPOINT+"?"+urlencode(params),timeout=45) as response:
                    body=response.read()
                    item["http_status"]=response.status
                file.write_bytes(body)
            item["response_sha256"]=digest(file)
            edb=normalize_edb(pd.read_csv(StringIO(body.decode("utf8"))))
            raw=args.gm_root/"raw"/(symbol.replace(".","_")+".parquet")
            gm=pd.read_parquet(raw)
            # Independent exchange calendar is pending. Compare exactly the stored
            # GM actual-contract clock of this audited trading-day case.
            audit=read(ROOT/"reports/v3_asof_roll_audit.json")
            key="DCE.EG" if symbol.startswith("DCE") else symbol.split(".")[0]+"."+symbol.split(".")[1][:2]
            roll=next(r for r in audit["products"][key]["rolls"] if r["trading_date"]==day)
            # Both EG legs share the FULL audited day including Friday night.
            # The legacy new-main series started at 09:01 precisely because its
            # night leg was missing. Using that series' start would hide this gap.
            begins={"2026-08-14":"2026-08-13T21:01:00+08:00",
                    "2026-08-11":"2026-08-11T09:01:00+08:00",
                    "2026-08-24":"2026-08-21T21:01:00+08:00"}
            begin=stamp(begins[day])
            item["scope_first_eob"]=begin.isoformat()
            item["scope_basis"]="FULL_AUDITED_DAY_INCLUDING_NIGHT; official historical session attestation pending"
            stop=stamp(day+"T15:00:00+08:00")
            gm["eob"]=[stamp(t) for t in gm.eob]
            gm=gm[gm.eob.between(begin,stop)]
            edb=edb[edb.eob.between(begin,stop)]
            item.update(gm_rows=len(gm),edb_rows=len(edb),gm_sha256=digest(raw))
            if gm.empty or edb.empty:
                item["status"]="NO_OVERLAP"
            else:
                table=compare(gm,edb,tick)
                table.insert(0,"trading_date",day)
                table["symbol"]=symbol
                table.to_csv(args.local_output/(symbol+"_"+day+"_diff.csv"),index=False)
                both=table[table.missing.eq("")]
                item.update(status="COMPARED",joined_rows=len(table),common_rows=len(both),
                            missing_gm=int(table.missing.eq("GM").sum()),missing_edb=int(table.missing.eq("EDB").sum()),
                            max_abs_ohlc_diff_ticks={k:float(both[k+"_diff_ticks"].abs().max()) for k in ("open","high","low","close")},
                            volume_mismatches=int(both.volume_diff.ne(0).sum()),oi_mismatches=int(both.end_oi_diff.ne(0).sum()))
                item["volume_sum_ratio_gm_over_edb"]=float(gm.volume.sum()/edb.volume.sum()) if edb.volume.sum() else None
                item["shift_close_diagnostics"]={}
                for shift in (-1,0,1):
                    shifted=edb.copy()
                    shifted.eob+=timedelta(minutes=shift)
                    match=gm[["eob","close"]].merge(shifted[["eob","close"]],on="eob",suffixes=("_gm","_edb"))
                    item["shift_close_diagnostics"][str(shift)]=dict(n=len(match),
                        unequal=int((match.close_gm-match.close_edb).abs().gt(tick/100).sum()))
                aggregated={}
                # Separately aggregate each raw source using the same frozen M15 clock.
                for source,frame in (("gm",gm),("edb",edb)):
                    frame=frame.copy()
                    frame["symbol"]=symbol
                    frame["frequency"]="60s"
                    if source=="edb":
                        frame["position"]=frame.close_oi
                    b=aggregate_bars(bars_from_records(frame.to_dict("records")),15,"900s")
                    item[source+"_m15_rows"]=len(b)
                    aggregated[source]=pd.DataFrame([dict(eob=x.eob,open=x.open,high=x.high,low=x.low,close=x.close) for x in b])
                m=aggregated["gm"].merge(aggregated["edb"],on="eob",suffixes=("_gm","_edb"))
                item["m15_max_abs_ohlc_diff_ticks"]={k:float(((m[k+"_gm"]-m[k+"_edb"])/tick).abs().max())
                                                      for k in ("open","high","low","close")}
        except (HTTPError,URLError,TimeoutError,ValueError,KeyError,OSError) as exc:
            item.update(status="SOURCE_REQUEST_FAILED",error=str(exc)[:300])
        report["cases"].append(item)
    write(args.report,report)
    print(json.dumps(report,ensure_ascii=False))
    return 0 if all(c["status"]=="COMPARED" for c in report["cases"]) else 3


if __name__=="__main__":
    raise SystemExit(main())
