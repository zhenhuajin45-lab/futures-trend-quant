"""Metadata/clock-only inventory. Does not request blind-window prices or labels."""
from pathlib import Path
from datetime import datetime, timezone
import json, os, sys
from io import StringIO
import requests
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,digest
from tools.forward_quality.edb_compare import normalize_edb


def main():
    out=ROOT/'data/cache/v71_source_inventory'
    out.mkdir(parents=True,exist_ok=True)
    result=dict(observed_at=datetime.now(timezone.utc).isoformat(),calendars={},mapping={},metadata={},probes=[],
        no_scoring_window_prices_requested=True,no_labels=True,no_orders=True,
        historical_available_at='UNKNOWN',historical_mapping_known_at='UNKNOWN')
    reg=read(ROOT/'tools/first_entry_v7/registration.json')
    from gm.api import set_token,get_trading_dates,fut_get_continuous_contracts,get_instruments
    set_token(os.environ['GM_TOKEN'])
    for ex in ('CZCE','DCE','GFEX','SHFE'):
        try: result['calendars'][ex]=get_trading_dates(ex,'2025-06-01','2026-05-06')
        except Exception as exc: result['calendars'][ex]=dict(error_type=type(exc).__name__)
    for cs in reg['new_sample']['universe']:
        try:
            rows=fut_get_continuous_contracts(cs,'2025-12-01','2026-04-24')
            result['mapping'][cs]=rows
            symbols=sorted({r['symbol'] for r in rows})
            try:
                meta=get_instruments(symbols=','.join(symbols),fields='symbol,listed_date,delisted_date',df=True)
                result['metadata'][cs]=meta.to_dict('records')
            except Exception as exc: result['metadata'][cs]=dict(error_type=type(exc).__name__)
            # The previous completed trading-day dominant is a research proxy, not a historical arrival clock.
            early=[r for r in rows if str(r['trade_date'])<'2026-01-05']
            symbol=early[-1]['symbol'] if early else (rows[0]['symbol'] if rows else None)
            if symbol:
                for day in ('2025-09-15','2025-12-15'):
                    params=dict(symbol=symbol,period=60,start_time=day+' 09:00:00',end_time=day+' 09:05:00',col='volume')
                    item=dict(csymbol=cs,symbol=symbol,day=day,request=params)
                    try:
                        response=requests.get('https://edb.shinnytech.com/md/kline',params=params,timeout=(5,12))
                        item['http_status']=response.status_code
                        if response.ok and response.text.startswith('datetime_nano,'):
                            path=out/(symbol.replace('.','_')+'_'+day+'.csv')
                            path.write_bytes(response.content)
                            frame=normalize_edb(pd.read_csv(StringIO(response.text)))
                            item.update(rows=len(frame),sha256=digest(path),complete_m15=0,
                                first_eob=None if frame.empty else frame.eob.min().isoformat(),
                                last_eob=None if frame.empty else frame.eob.max().isoformat())
                        else:item.update(status='NO_VALID_CSV',rows=0)
                    except requests.RequestException as exc:item.update(status='REQUEST_FAILED',error_type=type(exc).__name__,rows=0)
                    result['probes'].append(item)
        except Exception as exc: result['mapping'][cs]=dict(error_type=type(exc).__name__)
        (out/'metadata.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf8')
        print(cs,'metadata/clock inventory saved',flush=True)
    result['status']='INVENTORIED_NOT_QUALIFIED'
    result['existing_bigquant_environment_present']=any(os.environ.get(k) for k in ('BIGQUANT_TOKEN','BIGQUANT_API_KEY'))
    (out/'metadata.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf8')
    print('CLOCK_ONLY_INVENTORY_DONE',len(result['probes']),flush=True)


if __name__=='__main__': main()
