"""Bounded metadata/availability probe. No strategy, future labels or orders."""
from pathlib import Path
from datetime import datetime,timezone
import json,os,re,io,hashlib
import requests
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]


def main():
    out=ROOT/'data/cache/v8_independent_source_probe';out.mkdir(parents=True,exist_ok=False)
    result=dict(purpose='ADMISSION_ONLY_NO_PNL',candidate_score_dates=['2025-09-01','2025-12-31'],
        observed_at=datetime.now(timezone.utc).isoformat(),orders=False,subscriptions=False,
        independent_prices_scored=False,historical_mapping_known_at='UNKNOWN',quote_execution_certified=False,probes=[])
    from gm.api import set_token,get_trading_dates,fut_get_continuous_contracts,history
    set_token(os.environ['GM_TOKEN'])
    dates=get_trading_dates('SHFE','2025-01-01','2025-12-31')
    result['candidate_trading_days']=len([d for d in dates if '2025-09-01'<=str(d)[:10]<='2025-12-31'])
    mapping=fut_get_continuous_contracts(csymbol='SHFE.AG',start_date='2025-07-01',end_date='2025-12-31')
    (out/'calendar_mapping.json').write_text(json.dumps(dict(dates=dates,mapping=mapping),default=str),encoding='utf8')
    result['metadata_sha256']=hashlib.sha256((out/'calendar_mapping.json').read_bytes()).hexdigest()
    def symbol_for(day):
        return next(r['symbol'] for r in mapping if str(r['trade_date'])[:10]==day)
    for day,frequency in [('2025-07-01','60s'),('2025-09-01','60s'),('2025-09-01','tick')]:
        symbol=symbol_for(day);item=dict(source='GM',symbol=symbol,day=day,frequency=frequency)
        try:
            kwargs=dict(symbol=symbol,frequency=frequency,start_time=day+' 09:00:00',end_time=day+' 09:00:05' if frequency=='tick' else day+' 09:05:00',df=True)
            if frequency=='60s':kwargs.update(fields='symbol,bob,eob,volume',adjust=0)
            rows=history(**kwargs);item.update(status='RECEIVED' if len(rows) else 'NO_ROWS',rows=len(rows))
            if len(rows):
                path=out/(symbol.replace('.','_')+'_'+day+'_'+frequency+'.parquet');rows.to_parquet(path,index=False)
                item['local_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception as exc:
            message=str(exc);status=re.search(r'"status"\s*:\s*(\d+)',message)
            found=re.findall(r'202\d-\d\d-\d\d',message)
            item.update(status='UNAVAILABLE',error_type=type(exc).__name__,provider_status=int(status.group(1)) if status else None,
                permission_denied='ERR_NO_DATA_PERMISSION' in message,earliest_query_date=found[-1] if found else None)
        result['probes'].append(item)
    for day in ['2025-07-01','2025-09-01','2025-11-06']:
        symbol=symbol_for(day);item=dict(source='EDB_FREE',symbol=symbol,day=day,frequency='60s')
        try:
            response=requests.get('https://edb.shinnytech.com/md/kline',params=dict(period=60,symbol=symbol,
                start_time=day+' 09:00:00',end_time=day+' 09:05:00',col='volume'),timeout=(5,12))
            item['http_status']=response.status_code
            if response.ok and response.content.startswith(b'datetime_nano,'):
                frame=pd.read_csv(io.BytesIO(response.content));path=out/(symbol.replace('.','_')+'_'+day+'_edb.csv')
                path.write_bytes(response.content);item.update(status='CLOCK_ROWS_RECEIVED',rows=len(frame),local_sha256=hashlib.sha256(response.content).hexdigest())
            else:item.update(status='UNAVAILABLE',rows=0)
        except requests.RequestException as exc:item.update(status='UNAVAILABLE',error_type=type(exc).__name__)
        result['probes'].append(item)
    result.update(economic_validation='PENDING_HISTORICAL_DATA_AND_QUOTES',formal_blind_window_requested=False,
        v71_window_requested=False,auth_retry=False,newest_unreserved_days_available=2,
        newest_days_not_used_as_substitute='2026-11-03/04 too short; do not consume an independent test')
    path=ROOT/'reports/v8_roll_source_probe.json';path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
