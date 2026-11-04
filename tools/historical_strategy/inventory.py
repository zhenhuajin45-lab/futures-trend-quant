"""Calendar-proxy coverage inventory. Does not change the historical source view."""
import argparse
from collections import Counter
from pathlib import Path
import sys
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,stamp,digest


def contract_quality(raw,expected):
    actual=set(stamp(t) for t in raw.eob)
    boundaries=pd.DatetimeIndex(expected).ceil("15min")
    received=pd.Series([t in actual for t in expected],index=boundaries,dtype=int)
    grouped=received.groupby(level=0).agg(["sum","size"])
    complete=grouped["sum"].eq(grouped["size"])
    qualifying=complete & complete.cumsum().ge(1200)
    return {t:bool(ok) for t,ok in qualifying.items()},int(complete.sum())


def build(root):
    manifest=read(root/"manifest.json")
    products={}
    for csymbol,product in manifest["products"].items():
        schedule=read(root/(csymbol+"_schedule.json"))
        expected=[stamp(x["eob"]) for x in schedule]
        selection=read(root/(csymbol+"_selection.json"))
        selected_expected={stamp(x["eob"]):selection.get(x["trading_date"]) for x in schedule}
        received=set()
        eligible=[]
        contracts=[]
        for item in product["contracts"]:
            raw=pd.read_parquet(root/item["path"])
            actual=set(stamp(t) for t in raw.eob)
            quality,complete=contract_quality(raw,expected)
            begin,end=raw.eob.min(),raw.eob.max()
            chosen={t for t in actual if selected_expected.get(t)==item["symbol"]}
            received|=chosen
            eligible.extend(t for t,passed in quality.items() if passed and selected_expected.get(t)==item["symbol"])
            contracts.append(dict(symbol=item["symbol"],raw_rows=len(raw),complete_observed_m15=complete,
                                  selected_received_m1=len(chosen),qualified_selected_m15=sum(1 for t in quality if quality[t] and selected_expected.get(t)==item["symbol"]),
                                  missing_minutes_within_available_span=sum(t not in actual for t in expected if begin<=t<=end),
                                  research_sha256=item["research_sha256"],patches=item["patches"],augmentation=item["augmentation"]))
        missing=sorted(set(expected)-received)
        products[csymbol]=dict(expected_m1=len(expected),selected_received_m1=len(received),
            absent_selected_m1=len(missing),first_absent_eob=missing[0].isoformat() if missing else None,
            qualifying_selected_m15=len(eligible),first_qualifying_eob=min(eligible).isoformat() if eligible else None,
            last_qualifying_eob=max(eligible).isoformat() if eligible else None,contracts=contracts)
    return dict(manifest_sha256=digest(root/"manifest.json"),products=products,
        schedule_status="CURRENT_AUTHORIZED_METADATA_PROXY; EXCEPTIONAL_HISTORICAL_CLOSURES_UNVERIFIED",
        warmup_status="1200_COMPLETE_OBSERVED_M15_PER_ACTUAL_CONTRACT; HISTORICAL_ARRIVAL_CLOCK_UNKNOWN",
        historical_mapping_known_at="UNKNOWN",supplemented_original_rows=manifest["supplemented_original_rows"])


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--data-root",type=Path,required=True)
    p.add_argument("--summary",type=Path,required=True)
    a=p.parse_args()
    write(a.summary,build(a.data_root))
