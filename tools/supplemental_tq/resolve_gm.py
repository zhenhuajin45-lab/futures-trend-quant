"""Snapshot exact-day GM mapping for an explicit TQ trial, never a TQ main proxy."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.supplemental_tq.collect import actual_symbols


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--date",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():
        raise SystemExit("fresh mapping output required")
    from gm.api import set_token,fut_get_continuous_contracts
    set_token(os.environ["GM_TOKEN"])
    products=("CZCE.CF","CZCE.CJ","CZCE.SA","CZCE.SR","DCE.EG","DCE.JD","DCE.LH","DCE.M","DCE.V","GFEX.LC","SHFE.AG","SHFE.AU")
    mapping={}
    originals={}
    observed={}
    for csymbol in products:
        raw=fut_get_continuous_contracts(csymbol=csymbol,start_date=args.date,end_date=args.date)
        originals[csymbol]=raw
        rows=[r for r in raw if str(r["trade_date"])[:10]==args.date]
        symbols={r["symbol"] for r in rows}
        if len(symbols)!=1:
            raise RuntimeError("exact-day mapping absent or conflicting: "+csymbol)
        mapping[csymbol]=actual_symbols(list(symbols))[0]
        observed[csymbol]=datetime.now(timezone.utc).isoformat()
    value=dict(trading_date=args.date,mapping=mapping,raw_responses=originals,
               first_observed_this_request_at=observed,historical_first_publication="UNKNOWN")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding="utf8")
    print(json.dumps(dict(trading_date=args.date,mapping=mapping),ensure_ascii=False))


if __name__=="__main__":
    main()
