"""Evidence-only normalization of explicitly reported zero-trade historical minutes.

Not a general gap filler. No live-minute interpolation, forward price or inferred volume.
"""
from pathlib import Path
import hashlib,json,math
import pandas as pd


def normalize_no_trade_minutes(frame,manifest,root,symbol,cutoff):
    root=Path(root).resolve();path=Path(manifest).resolve()
    if not path.is_relative_to(root/'data/cache'):raise ValueError('warmup evidence must remain in private cache')
    proof=json.loads(path.read_text(encoding='utf8'))
    if proof.get('purpose')!='VERIFIED_ZERO_TRADE_WARMUP_ONLY':raise ValueError('unapproved warmup evidence purpose')
    f=frame.copy();f['eob']=pd.to_datetime(f.eob,utc=True).dt.tz_convert('Asia/Shanghai')
    additions=[];receipts=[]
    for item in proof['entries']:
        if item['symbol']!=symbol:continue
        t=pd.Timestamp(item['eob'])
        if t<max(f.eob.min(),pd.Timestamp('2026-05-08',tz='Asia/Shanghai')) or t>min(f.eob.max(),pd.Timestamp(cutoff).floor('min')):continue
        if (f.eob==t).any():continue  # Never replace any GM-returned bar.
        csv=(root/item['csv']).resolve()
        if not csv.is_relative_to(root/'data/cache') or hashlib.sha256(csv.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('warmup original/hash mismatch')
        e=pd.read_csv(csv)
        e['bob']=pd.to_datetime(e.datetime_nano,unit='ns',utc=True).dt.tz_convert('Asia/Shanghai')
        e['eob']=e.bob+pd.Timedelta(minutes=1)
        if e.eob.duplicated().any():raise ValueError('duplicate evidence minute')
        target=e.loc[e.eob==t];previous=f.loc[f.eob==t-pd.Timedelta(minutes=1)]
        if len(target)!=1 or len(previous)!=1:raise ValueError('no unique prior GM close/zero-trade evidence')
        row=target.iloc[0];prior=previous.iloc[0];price=float(prior.close)
        if not math.isfinite(price) or price<=0 or float(row.volume)!=0 or any(not math.isfinite(float(row[k])) or abs(float(row[k])-price)>1e-7 for k in ('open','high','low','close')):
            raise ValueError('unknown or non-flat minute cannot be normalized')
        if int(row.open_oi)!=int(row.close_oi) or int(row.close_oi)!=int(prior.position):raise ValueError('zero-trade open interest not corroborated')
        overlap=f.merge(e,on='eob',suffixes=('_gm','_edb'))
        if len(overlap)<4 or overlap[['close_gm','close_edb','volume_gm','volume_edb','position','close_oi']].isna().any().any() or (overlap.close_gm-overlap.close_edb).abs().gt(1e-7).any() or (overlap.position!=overlap.close_oi).any():
            raise ValueError('neighbor close/interest alignment failed')
        diff=overlap.volume_gm-overlap.volume_edb;changed=overlap.loc[diff.ne(0)]
        if diff.ne(0).any():
            deltas=diff.loc[diff.ne(0)]
            if len(changed)!=2 or sorted(deltas.tolist())!=[-1,1] or abs((changed.iloc[1].eob-changed.iloc[0].eob).total_seconds())!=60:
                raise ValueError('unexplained neighboring volume difference')
        additions.append(dict(symbol=symbol,frequency='60s',bob=t-pd.Timedelta(minutes=1),eob=t,
            open=price,high=price,low=price,close=price,volume=0.,amount=0.,position=int(row.close_oi)))
        receipts.append(dict(symbol=symbol,eob=t.isoformat(),price_origin='PRIOR_CLOSED_GM_PRICE_MATCHING_EXPLICIT_EDB_FLAT_BAR',
            volume_origin='EDB_EXPLICIT_ZERO',amount_origin='ZERO_DERIVED_FROM_REPORTED_ZERO_VOLUME',oi_origin='EDB_MATCHES_PRIOR_GM',
            evidence_sha256=item['sha256'],neighbor_close_and_oi_matched=True,neighbor_volume_boundary_differences=int(diff.ne(0).sum()),
            historical_arrival_clock='UNKNOWN',use='HISTORICAL_STATE_WARMUP_ONLY'))
    if additions:f=pd.concat([f,pd.DataFrame(additions)],ignore_index=True).sort_values('eob')
    return f,receipts
