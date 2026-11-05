"""Past-only session features. No future response, first-touch or P&L API exists."""
from datetime import timedelta
import hashlib
import numpy as np
import pandas as pd
import pyarrow.dataset as ds
from .common import ALLOWED_PRICE_COLUMNS,authorize_price_span,result_columns,sha


def read_prefixes(item,policy,days,signal_clock='14:30'):
    if not item.get('engineering_price_access') or not authorize_price_span(policy,item.get('min_event_at'),item.get('max_event_at'),item.get('purpose')):
        raise ValueError('protected price body blocked before loader')
    if sha(item['path'])!=item['sha256']:raise ValueError('source changed after inventory')
    dataset=ds.dataset(item['path'],format='parquet')
    if result_columns(dataset.schema.names):raise ValueError('result columns forbidden before materialization')
    if not set(ALLOWED_PRICE_COLUMNS)<=set(dataset.schema.names):raise ValueError('M1 required fields missing')
    predicate=None
    for day in days:
        begin=pd.Timestamp(day+'T09:01:00+08:00');end=pd.Timestamp(day+'T09:30:00+08:00');ref=pd.Timestamp(day+'T'+signal_clock+':00+08:00')
        part=((ds.field('eob')>=begin.to_pydatetime())&(ds.field('eob')<=end.to_pydatetime()))|(ds.field('eob')==ref.to_pydatetime())
        predicate=part if predicate is None else predicate|part
    if predicate is None:return pd.DataFrame(columns=ALLOWED_PRICE_COLUMNS)
    # Both column projection and timestamp predicate are passed before materialization.
    columns=list(ALLOWED_PRICE_COLUMNS)+(['received_at'] if 'received_at' in dataset.schema.names else [])
    return dataset.to_table(columns=columns,filter=predicate).to_pandas()


def trading_minutes(expected,start,end):
    ends=pd.to_datetime(expected,utc=True);opens=ends-pd.Timedelta(minutes=1)
    return int(((opens>=pd.Timestamp(start))&(opens<pd.Timestamp(end))).sum())


def require_obi5_fields(columns):
    required={f'{side}_{kind}_{level}' for side in ('bid','ask') for kind in ('price','size') for level in range(1,6)}|{'event_at','received_at'}
    missing=sorted(required-set(columns))
    if missing:raise ValueError('OBI5 not testable from M1/L1: missing '+','.join(missing))
    return dict(schema_only=True,continuous_coverage_verified=False)


def session_observation(frame,expected,symbol,day,tick=1.):
    if result_columns(frame.columns):raise ValueError('outcome/result fields cannot be accessed by preflight')
    ready=pd.Timestamp(day+'T14:30:00+08:00');morning=pd.date_range(day+'T09:01:00+08:00',periods=30,freq='min')
    # The API will not inspect prices after the known feature cutoff.
    f=frame.loc[(frame.symbol==symbol)&(pd.to_datetime(frame.eob,utc=True)<=ready)].copy()
    f['eob']=pd.to_datetime(f.eob,utc=True).dt.tz_convert('Asia/Shanghai');duplicates=bool(f.eob.duplicated().any())
    a=f.loc[f.eob.isin(morning)];reference=f.loc[f.eob==ready]
    issues=[]
    if duplicates:issues.append('DUPLICATE_TIMESTAMP')
    if len(a)!=30 or set(a.eob)!=set(morning) or len(reference)!=1:issues.append('MISSING_FEATURE_MINUTES')
    if 'received_at' in f.columns:
        needed=f.loc[f.eob.isin(morning)|(f.eob==ready)]
        reception=pd.to_datetime(needed.received_at,utc=True,errors='coerce')
        naive=any(not pd.isna(x) and pd.Timestamp(x).tzinfo is None for x in needed.received_at)
        if naive or reception.isna().any() or (reception>ready).any():issues.append('UNKNOWN_OR_LATE_INPUT_AT_SIGNAL')
    result=dict(day=day,symbol=symbol,event_at=ready.isoformat(),direction_evidence_event_at=morning[-1].isoformat(),received_at='UNKNOWN',signal_ready_at=ready.isoformat(),
        order_eligible_at=ready.isoformat(),fill_model_at='NOT_COMPUTED',clock_basis='EVENT_TIME_PROXY',
        direction=None,control_direction=None,risk_valid=None,control_risk_valid=None,feature_complete=False,
        historical_mapping_known_at='UNKNOWN; OLD_RESEARCH_ASSUMPTION',latency_status='COMPUTE_ONLY_MEASURABLE_BROKER_DELAY_UNKNOWN',issues=issues)
    ends=pd.to_datetime(expected,utc=True).tz_convert('Asia/Shanghai');opens=ends-pd.Timedelta(minutes=1)
    expiry=pd.Timestamp(day+'T14:55:00+08:00');eligible=opens[(opens>ready)&(opens<expiry)]
    result.update(next_legal_model_entry_at=eligible[0].isoformat() if len(eligible) else None,
        available_tradable_minutes=trading_minutes(expected,eligible[0],expiry) if len(eligible) else 0,
        expiry_at=expiry.isoformat(),natural_minutes_to_expiry=float((expiry-eligible[0]).total_seconds()/60) if len(eligible) else None)
    required=pd.date_range(day+'T14:31:00+08:00',periods=24,freq='min')
    result['adapted_window_compatible']=bool(len(eligible)==24 and not opens.duplicated().any() and set(eligible)==set(required))
    result['window_issue']=None if result['adapted_window_compatible'] else ('NO_ENTRY_WINDOW' if not len(eligible) else 'WINDOW_MISMATCH')
    if issues:return result
    a=a.sort_values('eob');values=a[['open','high','low','close']].to_numpy()
    if not np.isfinite(values).all() or (a.high<a.low).any() or (a.open<a.low).any() or (a.open>a.high).any() or (a.close<a.low).any() or (a.close>a.high).any() or not np.isfinite(reference.close.iloc[0]):
        result['issues']=['INVALID_PAST_PRICE'];return result
    direction=1 if a.close.iloc[-1]>a.open.iloc[0] else -1 if a.close.iloc[-1]<a.open.iloc[0] else 0
    placebo=1 if int.from_bytes(hashlib.sha256(('20261105|'+symbol+'|'+day).encode()).digest()[:8],'big')%2 else -1
    ref=float(reference.close.iloc[0]);low=float(a.low.min())-tick;high=float(a.high.max())+tick
    result.update(candidate_risk_ticks=(ref-low if direction==1 else high-ref)/tick if direction else None,
        control_risk_ticks=(ref-low if placebo==1 else high-ref)/tick)
    result.update(feature_complete=True,direction=direction,control_direction=placebo,
        risk_valid=bool(direction and (ref-low if direction==1 else high-ref)>0),
        control_risk_valid=bool((ref-low if placebo==1 else high-ref)>0),nonzero_direction=direction!=0)
    return result


def classify_sequence(event_times,received_times,ready_times,event_end_times):
    """Quality and nonoverlap helper; no price field is accepted."""
    events=pd.to_datetime(event_times,utc=True);received=pd.to_datetime(received_times,utc=True);ready=pd.to_datetime(ready_times,utc=True)
    seen=set();last_event=None;until=None;rows=[]
    ends=pd.to_datetime(event_end_times,utc=True)
    original=list(zip(event_times,received_times,ready_times,event_end_times))
    for i,(e,r,s,end) in enumerate(zip(events,received,ready,ends)):
        unknown=any(pd.isna(t) or pd.Timestamp(t).tzinfo is None for t in original[i])
        duplicate=e in seen;late=bool(not unknown and (r>s or e>s));out_of_order=bool(last_event is not None and not pd.isna(e) and e<last_event)
        accepted=not unknown and not duplicate and not late and not out_of_order and (until is None or s>=until)
        rows.append(dict(duplicate=duplicate,late=late,out_of_order=out_of_order,unknown_clock=unknown,accepted=accepted))
        seen.add(e)
        if not pd.isna(e):last_event=e if last_event is None else max(e,last_event)
        if accepted:until=end
    return rows
