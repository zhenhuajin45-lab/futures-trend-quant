"""Preserve three requested actual legs; report gaps, never qualify or score them."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from io import StringIO
import json,os,re
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from backtest.forward_v3 import read,write,digest
from tools.forward_quality.edb_compare import normalize_edb
from tools.first_entry_v71.clock import dates,calendar_clock,anchor_rows,timestamp,DAY_SESSIONS,NIGHT_END

ROOT=Path(__file__).resolve().parents[2]
COLS='open,high,low,close,volume,open_oi,close_oi'
LEGS=('DCE.jd2602','DCE.eg2605','DCE.jd2605')


def fetch_edb(item,folder):
    symbol,start,end=item
    params=dict(symbol=symbol,period=60,start_time=start,end_time=end,col=COLS)
    row=dict(source='EDB_HTTP_FREE',symbol=symbol,request=params,
             requested_at=datetime.now(timezone.utc).isoformat())
    try:
        response=requests.get('https://edb.shinnytech.com/md/kline',params=params,timeout=(5,30))
        valid=response.ok and response.content.startswith(b'datetime_nano,')
        name=symbol.replace('.','_')+'_'+start[:10]+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        path=folder/(name+('.csv' if valid else '.response'))
        with path.open('xb') as stream:stream.write(response.content)
        row.update(http_status=response.status_code,response_file=path.name,response_sha256=digest(path),
                   bytes=len(response.content),received_at=datetime.now(timezone.utc).isoformat(),
                   server_date=response.headers.get('Date'),status='RAW_PRESERVED_NOT_QUALIFIED' if valid else 'NO_VALID_M1')
        if valid:row['rows']=len(pd.read_csv(StringIO(response.text)))
    except requests.RequestException as exc:row.update(status='REQUEST_FAILED',error_type=type(exc).__name__,rows=0)
    return row


def gm_permission_checks(folder):
    from gm.api import set_token,history,ADJUST_NONE
    if not os.environ.get('GM_TOKEN'):return [dict(source='GM_HISTORY',status='NO_CONFIGURED_TOKEN')]
    set_token(os.environ['GM_TOKEN']);result=[]
    for symbol,start,end in [('DCE.jd2602','2025-09-03 09:00:00','2025-09-03 09:15:00'),
        ('DCE.eg2605','2025-10-20 14:00:00','2025-10-20 14:15:00'),
        ('DCE.jd2605','2025-11-04 09:00:00','2025-11-04 09:15:00')]:
        params=dict(symbol=symbol,frequency='60s',start_time=start,end_time=end,
            fields='symbol,bob,eob,open,high,low,close,volume,position',skip_suspended=False,
            fill_missing=None,adjust=ADJUST_NONE,adjust_end_time='',df=True)
        row=dict(source='GM_HISTORY',symbol=symbol,request=params,
            requested_at=datetime.now(timezone.utc).isoformat())
        try:
            frame=history(**params);row.update(status='RECEIVED',rows=len(frame))
            if len(frame):
                path=folder/(symbol.replace('.','_')+'_'+start[:10]+'_gm.parquet')
                if path.exists():raise FileExistsError('existing GM response; use a new acquisition directory')
                frame.to_parquet(path,index=False);row.update(response_file=path.name,response_sha256=digest(path))
        except Exception as exc:
            code=re.search(r'\b(2002|2003|1202)\b',str(exc))
            row.update(status='REQUEST_NOT_GRANTED_OR_FAILED',error_type=type(exc).__name__,
                sdk_code=code.group(1) if code else 'UNKNOWN',rows=0)
        result.append(row)
    return result


def proxy_clock(meta,cs):
    opened=set(meta['calendars'][cs.split('.')[0]]);records=[];previous=None
    for day in dates('2025-06-01','2026-05-06'):
        is_open=day in opened
        night=previous if is_open and previous and cs in NIGHT_END and (pd.Timestamp(day)-pd.Timestamp(previous)).days<=3 else None
        row=dict(date=day,is_open=is_open,night_start_date=night)
        if is_open and cs in NIGHT_END and night is None:row['night_closed_reason']='VERIFIED_HOLIDAY_REOPEN'
        records.append(row)
        if is_open:previous=day
    # The enum above satisfies the existing clock parser; it is NOT an authority attestation.
    bundle=dict(coverage_start='2025-06-01',coverage_end='2026-05-06',products={cs:dict(days=records,
        day_sessions=[list(x) for x in DAY_SESSIONS],night_end=NIGHT_END.get(cs))})
    return calendar_clock(bundle,cs)


def intervals(values):
    result=[]
    for t in sorted(values):
        if result and t-timestamp(result[-1]['last_eob'])==pd.Timedelta(minutes=1):
            result[-1].update(last_eob=t.isoformat(),minutes=result[-1]['minutes']+1)
        else:result.append(dict(first_eob=t.isoformat(),last_eob=t.isoformat(),minutes=1))
    return result


def merge_sources(rows,folder,symbol):
    chunks=[];source_hashes=[]
    for row in rows:
        if row.get('symbol')!=symbol or not row.get('response_file','').endswith('.csv'):continue
        path=folder/row['response_file']
        if digest(path)!=row['response_sha256']:raise ValueError('raw response changed: '+path.name)
        frame=normalize_edb(pd.read_csv(path))
        values=frame[COLS.split(',')].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (frame[['volume','open_oi','close_oi']]<0).any().any():raise ValueError('invalid raw numbers')
        if (frame.high<frame[['open','low','close']].max(axis=1)).any() or (frame.low>frame[['open','high','close']].min(axis=1)).any():raise ValueError('invalid raw OHLC')
        if (frame.bob!=frame.bob.dt.floor('min')).any():raise ValueError('non-minute raw timestamp')
        chunks.append(frame);source_hashes.append(dict(file=path.name,sha256=row['response_sha256']))
    if not chunks:return pd.DataFrame(),source_hashes,0
    combined=pd.concat(chunks,ignore_index=True)
    duplicated=combined[combined.eob.duplicated(keep=False)]
    if len(duplicated) and duplicated.groupby('eob')[COLS.split(',')].nunique().gt(1).any().any():
        raise ValueError('conflicting duplicate minute')
    duplicates=int(combined.eob.duplicated().sum())
    return combined.drop_duplicates('eob').sort_values('eob'),source_hashes,duplicates


def make_receipts(meta,gaps,probes,folder):
    clocks={};out=[]
    for symbol in LEGS:
        leg=next(r for r in gaps['rows'] if r['actual_contract']==symbol)
        cs=leg['csymbol']
        if cs not in clocks:clocks[cs]=proxy_clock(meta,cs)
        first=timestamp(leg['first_expected_scoring_eob_proxy'])
        ends=[timestamp(r['eob']) for r in anchor_rows(clocks[cs]) if timestamp(r['eob'])<first][-1200:]
        assert len(ends)==1200
        expected={end-pd.Timedelta(minutes=k) for end in ends for k in range(15)}
        frame,hashes,duplicates=merge_sources(probes,folder,symbol)
        observed=set(frame.eob) if len(frame) else set()
        complete=sum(all(end-pd.Timedelta(minutes=k) in observed for k in range(15)) for end in ends)
        missing_path=folder/(symbol.replace('.','_')+'_missing_eobs.csv')
        pd.DataFrame(dict(eob=[t.isoformat() for t in sorted(expected-observed)])).to_csv(missing_path,index=False)
        normalized=None
        if len(frame):
            frame=frame.copy();frame['symbol']=symbol;frame['position']=frame.close_oi
            normalized_path=folder/(symbol.replace('.','_')+'_normalized_research_only.parquet')
            frame.to_parquet(normalized_path,index=False)
            normalized=dict(file=normalized_path.name,sha256=digest(normalized_path))
        out.append(dict(actual_contract=symbol,csymbol=cs,status='READY_FOR_DATA',
            first_scoring_anchor_proxy=first.isoformat(),required_complete_m15=1200,qualified_complete_m15=0,
            expected_minutes_proxy=len(expected),received_required_minutes=len(expected&observed),
            complete_m15_proxy=complete,missing_m15_proxy=1200-complete,
            missing_minutes_proxy=len(expected-observed),unexpected_observed_minutes=len(observed-expected),
            missing_intervals_proxy=intervals(expected-observed),raw_files=hashes,
            missing_minute_file=dict(file=missing_path.name,sha256=digest(missing_path)),normalized_research_file=normalized,
            identical_overlap_rows=duplicates,conflicting_duplicate_rows=0,
            zero_volume_minutes=int(frame.volume.eq(0).sum()) if len(frame) else 0,
            historical_session_authority='PENDING; GM_DATE_CALENDAR_PLUS_UNVERIFIED_STANDARD_SESSIONS',
            historical_arrival_at='UNKNOWN',historical_mapping_known_at='UNKNOWN',source_calibration='PENDING',
            proof_of_1200_qualified_m15=False))
    return out


def main():
    p=argparse.ArgumentParser();p.add_argument('--local-output',type=Path,required=True)
    p.add_argument('--initial-probes',type=Path,required=True);p.add_argument('--public-report',type=Path,required=True)
    p.add_argument('--gm-check',action='store_true');p.add_argument('--reuse-acquisition',action='store_true');a=p.parse_args()
    initial=read(a.initial_probes);probes=initial['probes'];a.local_output.mkdir(parents=True,exist_ok=True)
    if a.local_output.resolve()!=a.initial_probes.parent.resolve():raise ValueError('keep immutable initial raw files in the same local acquisition root')
    acquisition=a.local_output/'acquisition_manifest.json'
    if acquisition.exists() and not a.reuse_acquisition:raise ValueError('acquisition already recorded; no implicit refresh')
    # Preserve only the specifically requested boundary leg, plus pre-scoring source controls.
    requests_to_make=[('DCE.jd2605','2025-11-05 09:00:00','2025-11-30 15:00:00'),
        ('DCE.jd2605','2025-12-01 09:00:00','2025-12-31 15:00:00'),
        ('DCE.jd2605','2026-01-01 09:00:00','2026-01-31 15:00:00'),
        ('DCE.jd2605','2026-02-01 09:00:00','2026-03-05 15:00:00'),
        ('DCE.jd2602','2025-11-05 09:00:00','2025-11-05 15:00:00'),
        ('DCE.eg2605','2025-11-04 21:00:00','2025-11-05 15:00:00')]
    if a.reuse_acquisition:
        manifest=read(acquisition)
        if manifest['initial_manifest_sha256']!=digest(a.initial_probes):raise ValueError('initial receipt changed')
        probes=manifest['probes'];gm=manifest['gm_permission_checks']
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:probes+=list(pool.map(lambda item:fetch_edb(item,a.local_output),requests_to_make))
        gm=gm_permission_checks(a.local_output) if a.gm_check else []
        manifest=dict(observed_at=datetime.now(timezone.utc).isoformat(),purpose='WARMUP_ACQUISITION_ONLY',
            initial_manifest_sha256=digest(a.initial_probes),probes=probes,gm_permission_checks=gm,
            no_labels=True,no_strategy_replay=True,no_orders=True)
        write(acquisition,manifest)
    metadata=ROOT/'data/cache/v71_source_inventory/metadata.json'
    receipts=make_receipts(read(metadata),read(ROOT/'reports/v71_actual_leg_data_gaps.json'),probes,a.local_output)
    report=dict(status='READY_FOR_DATA',purpose='RAW_COVERAGE_AND_GAPS_ONLY',
        acquisition_manifest_sha256=digest(a.local_output/'acquisition_manifest.json'),
        independent_gm_date_metadata_sha256=digest(metadata),probes=probes,gm_permission_checks=gm,legs=receipts,
        normalization=dict(timezone='Asia/Shanghai',provider_timestamp='M1_START_UNIX_NS',eob='BOB_PLUS_60_SECONDS',
            volume='LOTS',position='close_oi',zero_volume='PRESERVE_ORIGINAL',price='UNADJUSTED_ACTUAL_CONTRACT'),
        no_real_scoring_runs=True,no_candidate_verdict=True,no_orders=True,no_fee_or_net_returns=True,
        not_formal_forward_data=True)
    write(a.public_report,report)
    print(json.dumps(dict(status=report['status'],legs=[{k:r[k] for k in ('actual_contract','received_required_minutes','complete_m15_proxy','qualified_complete_m15','missing_minutes_proxy')} for r in receipts])))


if __name__=='__main__':main()
