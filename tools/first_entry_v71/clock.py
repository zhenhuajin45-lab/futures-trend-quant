"""Calendar-derived minutes; a supplied bar schedule is never the denominator."""
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
import json
import pandas as pd
import numpy as np
from zoneinfo import ZoneInfo
CHINA=ZoneInfo('Asia/Shanghai')

DAY_SESSIONS=(('09:00','10:15'),('10:30','11:30'),('13:30','15:00'))
NIGHT_END={'CZCE.CF':'23:00','CZCE.SA':'23:00','CZCE.SR':'23:00','DCE.EG':'23:00',
           'DCE.M':'23:00','DCE.V':'23:00','SHFE.AG':'02:30','SHFE.AU':'02:30'}


class AdmissionError(ValueError):
    def __init__(self, code, **detail):
        self.code,self.detail=code,detail
        super().__init__(code+': '+json.dumps(detail,ensure_ascii=False,default=str))


def object_hash(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def dates(start,end):
    current=date.fromisoformat(start)
    while current<=date.fromisoformat(end):
        yield current.isoformat()
        current+=timedelta(days=1)


def timestamp(value):
    result=pd.Timestamp(value)
    if result.tzinfo is None:
        raise AdmissionError('TIMEZONE_MISSING',value=str(value))
    return result.tz_convert(CHINA)


def time_index(values):
    return pd.DatetimeIndex(pd.to_datetime(values,format='ISO8601',utc=True)).tz_convert(CHINA)


def calendar_clock(bundle,csymbol):
    """Explicit open/closed record for every civil day, independently attested.

    Night start is an actual civil date, not trading_date minus one. Standard
    templates cannot be shortened by omitting minutes from a raw/schedule file.
    """
    product=bundle['products'][csymbol]
    records=product['days']
    supplied=[r['date'] for r in records]
    if supplied!=list(dates(bundle['coverage_start'],bundle['coverage_end'])):
        raise AdmissionError('CALENDAR_DAY_COVERAGE',csymbol=csymbol)
    if product.get('day_sessions')!=[list(x) for x in DAY_SESSIONS]:
        raise AdmissionError('DAY_TEMPLATE_NOT_VERIFIED',csymbol=csymbol)
    if product.get('night_end')!=NIGHT_END.get(csymbol):
        raise AdmissionError('NIGHT_TEMPLATE_NOT_VERIFIED',csymbol=csymbol)
    minutes=[]
    previous_open=None
    for record in records:
        day=record['date']
        if not record['is_open']:
            continue
        segments=[]
        night=record.get('night_start_date')
        if night is not None:
            if csymbol not in NIGHT_END or night!=previous_open:
                raise AdmissionError('NIGHT_DATE_INVALID',csymbol=csymbol,day=day)
            begin=timestamp(night+'T21:00:00+08:00')
            end=timestamp(night+'T'+NIGHT_END[csymbol]+':00+08:00')
            if end<begin:end+=pd.Timedelta(days=1)
            segments.append((begin,end,'night'))
        elif csymbol in NIGHT_END and previous_open is not None:
            if record.get('night_closed_reason')!='VERIFIED_HOLIDAY_REOPEN':
                raise AdmissionError('UNEXPLAINED_MISSING_NIGHT',csymbol=csymbol,day=day)
        for i,(a,b) in enumerate(DAY_SESSIONS):
            segments.append((timestamp(day+'T'+a+':00+08:00'),timestamp(day+'T'+b+':00+08:00'),str(i)))
        for begin,end,session in segments:
            if end<=begin or (end-begin).total_seconds()%900:
                raise AdmissionError('UNREGISTERED_SHORT_SESSION',csymbol=csymbol,day=day)
            for eob in pd.date_range(begin+pd.Timedelta(minutes=1),end,freq='min'):
                minutes.append(dict(eob=eob.isoformat(),trading_date=day,session=session))
        previous_open=day
    eobs=time_index([r['eob'] for r in minutes])
    if eobs.has_duplicates or not eobs.is_monotonic_increasing:
        raise AdmissionError('CALENDAR_MINUTES_NOT_UNIQUE_MONOTONIC',csymbol=csymbol)
    return minutes


def anchor_rows(minutes):
    """Only independently expected 15-minute complete buckets are M15 anchors."""
    if not minutes:return []
    eobs=time_index([r['eob'] for r in minutes]);buckets=eobs.ceil('15min')
    ends=np.flatnonzero(np.r_[buckets.asi8[1:]!=buckets.asi8[:-1],True])
    starts=np.r_[0,ends[:-1]+1]
    result=[]
    for start,index in zip(starts,ends):
        end=buckets[index]
        if index-start+1!=15 or eobs[index]!=end:
            raise AdmissionError('CALENDAR_SHORT_M15_BUCKET',end=end.isoformat(),minutes=int(index-start+1))
        result.append(dict(eob=end.isoformat(),trading_date=minutes[index]['trading_date'],minutes=15))
    return result


def make_lock(bundle,registration,registration_sha256,calendar_sha256,clocks=None,only_product=None):
    start,end=registration['new_sample']['proposed_scoring_trade_dates']
    if set(bundle['products'])!=set(registration['new_sample']['universe']):
        raise AdmissionError('CALENDAR_UNIVERSE_CHANGED')
    if bundle['coverage_start']>registration['new_sample']['proposed_prehistory_start'] or bundle['coverage_end']!=registration['new_sample']['proposed_price_path_end'][:10]:
        raise AdmissionError('CALENDAR_RANGE_INSUFFICIENT')
    purpose=bundle.get('purpose','')
    if purpose not in ('INDEPENDENT_RESEARCH','ENGINEERING_FIXTURE_ONLY'):
        raise AdmissionError('CALENDAR_PURPOSE_UNKNOWN')
    if not bundle.get('independent_of_quote_minutes') or not bundle.get('evidence_files'):
        raise AdmissionError('CALENDAR_SOURCE_EVIDENCE_MISSING')
    if purpose=='INDEPENDENT_RESEARCH':
        attestations=bundle.get('historical_attestations',[])
        proof_hashes={p['sha256'] for p in bundle['evidence_files']}
        for exchange in {s.split('.')[0] for s in bundle['products']}:
            for kind in ('calendar','holiday_night_closures','historical_sessions'):
                qualified=[r for r in attestations if r.get('exchange')==exchange and r.get('kind')==kind and
                    r.get('status')=='VERIFIED' and r.get('evidence_sha256') in proof_hashes and
                    r.get('covers_start','9999')<=bundle['coverage_start'] and r.get('covers_end','0000')>=bundle['coverage_end']]
                if not qualified:raise AdmissionError('HISTORICAL_AUTHORITY_EVIDENCE_UNVERIFIED',exchange=exchange,kind=kind)
        policy=bundle.get('source_policy',{})
        required=('source','mapping','volume','position','zero_volume','timezone','raw_time_label',
                  'normalized_time_label','price_adjustment','calibration_sha256','arrival_at','mapping_known_at')
        if any(not policy.get(key) for key in required):
            raise AdmissionError('SOURCE_POLICY_NOT_FULLY_FROZEN')
        if (policy['volume']!='LOTS' or policy['position']!='END_OI' or policy['timezone']!='Asia/Shanghai' or
            policy['normalized_time_label']!='CLOSED_M1_EOB' or policy['price_adjustment']!='UNADJUSTED_ACTUAL_CONTRACT' or
            policy['calibration_sha256'] not in proof_hashes):
            raise AdmissionError('SOURCE_NORMALIZATION_OR_CALIBRATION_NOT_VERIFIED')
    products={}
    for cs in sorted(bundle['products']) if only_product is None else [only_product]:
        minutes=calendar_clock(bundle,cs) if clocks is None else clocks[cs]
        anchors=[r for r in anchor_rows(minutes) if start<=r['trading_date']<=end]
        days=sorted({r['trading_date'] for r in anchors})
        if not days or days[0]!=start or days[-1]!=end:
            raise AdmissionError('REGISTERED_FIRST_OR_LAST_DAY_MISSING',csymbol=cs)
        if purpose=='INDEPENDENT_RESEARCH':
            date_lock=json.loads((Path(__file__).resolve().parents[2]/'config/v71_trade_date_lock.json').read_text(encoding='utf8'))
            if days!=date_lock['trade_dates_by_exchange'][cs.split('.')[0]]:
                raise AdmissionError('INDEPENDENT_DATE_LOCK_MISMATCH',csymbol=cs)
        products[cs]=dict(scoring_trade_dates=days,scoring_anchor_eobs=[r['eob'] for r in anchors],
            clock_sha256=object_hash(minutes),scoring_anchor_sha256=object_hash(anchors),
            expected_scoring_m15=len(anchors),day_sessions=bundle['products'][cs]['day_sessions'],
            night_end=bundle['products'][cs]['night_end'])
    starts=[p['scoring_anchor_eobs'][0] for p in products.values()]
    ends=[p['scoring_anchor_eobs'][-1] for p in products.values()]
    result=dict(status='FROZEN_BEFORE_LABELS',purpose=purpose,registration_sha256=registration_sha256,
        calendar_sha256=calendar_sha256,products=products,
        first_scoring_eob=min(starts,key=timestamp),last_scoring_eob=max(ends,key=timestamp),
        last_path_eob=registration['new_sample']['proposed_price_path_end'],
        source_policy=bundle['source_policy'],input_file_sha256=bundle.get('input_file_sha256',{}))
    if purpose=='INDEPENDENT_RESEARCH':
        from tools.historical_strategy.reproduce import canonical_digest
        root=Path(__file__).resolve().parents[2]
        files=['tools/first_entry_v71/'+name for name in ('clock.py','admission.py','entrypoint.py','metrics.py','scoring.py')]
        files+=['tools/first_entry_v7/selector.py','tools/first_entry_v7/symmetric.py',
                'config/fees.json','config/v71_trade_date_lock.json']
        result['research_source_canonical_sha256']={name:canonical_digest(root/name) for name in files}
        result['production_source_lock_canonical_sha256']=canonical_digest(root/'reports/v6_reproduction_lock.json')
    return result
