"""Offline exact raw-range checks, non-destructive union and pending-source receipt."""
import argparse
from pathlib import Path
import shutil
import pandas as pd
from backtest.forward_v3 import read,write,digest,check_lock
from tools.first_entry_v71.fetch_warmup_v712 import expected_minutes,inspect_csv,FULL,RECEIPT_SHA,sha
from tools.first_entry_v71.warmup_receipts import make_receipts
from tools.historical_strategy.reproduce import canonical_digest

ROOT=Path(__file__).resolve().parents[2]


def day_minutes(day):
    times=[]
    for start,end in [('09:01','10:15'),('10:31','11:30'),('13:31','15:00')]:
        times+=list(pd.date_range(day+' '+start,day+' '+end,freq='min',tz='Asia/Shanghai'))
    return set(times)


def check_jd_candidate(candidate,reference):
    """BigQuant EOB schema, both Nov04/05 required. No source policy or GO signing."""
    required={'instrument','date','open','high','low','close','volume','open_interest'}
    if not required.issubset(candidate.columns) or set(candidate.instrument)!={'jd2605.DCE'}:
        raise ValueError('wrong contract or missing full candidate fields')
    frame=candidate.copy();times=pd.DatetimeIndex(pd.to_datetime(frame.date))
    frame['eob']=times.tz_localize('Asia/Shanghai') if times.tz is None else times.tz_convert('Asia/Shanghai')
    if frame.eob.duplicated().any() or not frame.eob.is_monotonic_increasing:raise ValueError('candidate times not unique/sorted')
    numeric=['open','high','low','close','volume','open_interest']
    import numpy as np
    if not np.isfinite(frame[numeric].to_numpy(dtype=float)).all() or (frame[['volume','open_interest']]<0).any().any():
        raise ValueError('invalid candidate quantities')
    if (frame.high<frame[['open','low','close']].max(axis=1)).any() or (frame.low>frame[['open','high','close']].min(axis=1)).any():
        raise ValueError('invalid candidate OHLC')
    expected=day_minutes('2025-11-04')|day_minutes('2025-11-05')
    if set(frame.eob)!=expected:raise ValueError('both candidate days must have exact 225 minutes')
    ref=reference.copy();ref['eob']=pd.DatetimeIndex(ref.eob).tz_convert('Asia/Shanghai')
    ref=ref[ref.eob.isin(day_minutes('2025-11-05'))]
    if ref.eob.duplicated().any() or set(ref.eob)!=day_minutes('2025-11-05'):raise ValueError('reference day incomplete')
    candidate_day=frame[frame.eob.isin(day_minutes('2025-11-05'))].set_index('eob')
    ref=ref.set_index('eob');table=pd.DataFrame(index=ref.index)
    for field in numeric:
        reference_field='close_oi' if field=='open_interest' else field
        table[field+'_difference']=candidate_day[field]-ref[reference_field]
    equal=table.eq(0).all(axis=1)
    return dict(status='OVERLAP_EQUAL_AWAITING_SOURCE_AND_CLOCK_REVIEW' if equal.all() else 'OVERLAP_MISMATCH_BLOCKED',
        matched_minutes=225,equal_minutes=int(equal.sum()),different_minutes=int((~equal).sum()),
        missing_day_minutes=225,may_proceed_to_source_review=bool(equal.all()),strict_qualified_legs=0),table.reset_index()


def main():
    p=argparse.ArgumentParser();p.add_argument('--batch',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    a=p.parse_args()
    if a.output.exists():raise ValueError('use a new immutable union directory')
    baseline_path=ROOT/'reports/v711_warmup_receipts.json';baseline=read(baseline_path)
    assert sha(baseline_path.read_bytes().replace(b'\r\n',b'\n'))==RECEIPT_SHA
    receipt=read(a.batch/'receipt.json');legs={r['actual_contract']:r for r in baseline['legs']}
    assert len(receipt['legs'])==2 and {r['symbol'] for r in receipt['legs']}=={'DCE.jd2602','DCE.eg2605'}
    a.output.mkdir(parents=True)
    original_root=ROOT/'data/cache/v711_warmup';manifest=read(original_root/'acquisition_manifest.json')
    probes=list(manifest['probes']);checked=[]
    for row in probes:
        if not row.get('response_file'):continue
        source=original_root/row['response_file']
        assert source.resolve().is_relative_to(original_root.resolve()) and digest(source)==row['response_sha256']
        shutil.copyfile(source,a.output/source.name)
    for row in receipt['legs']:
        assert row['status']=='RAW_RANGE_CHECKED'
        source=(a.batch/row['raw_file']).resolve()
        assert source.is_relative_to(a.batch.resolve()) and digest(source)==row['response_sha256']
        stats=inspect_csv(source.read_bytes(),expected_minutes(legs[row['symbol']]),FULL)
        assert stats['missing']==stats['extra']==stats['duplicates']==0 and stats['time_sorted']
        target=a.output/source.name
        if target.exists():raise ValueError('raw filename collision')
        shutil.copyfile(source,target)
        probes.append(dict(source='EDB_HTTP_FREE',symbol=row['symbol'],request=row['request'],
            response_file=source.name,response_sha256=row['response_sha256'],rows=stats['rows']))
        checked.append(dict(symbol=row['symbol'],sha256=row['response_sha256'],**stats))
    write(a.output/'union_manifest.json',dict(baseline_manifest_sha256=digest(original_root/'acquisition_manifest.json'),
        supplement_receipt_sha256=digest(a.batch/'receipt.json'),probes=probes))
    result=make_receipts(read(ROOT/'data/cache/v71_source_inventory/metadata.json'),
        read(ROOT/'reports/v71_actual_leg_data_gaps.json'),probes,a.output)
    expected_counts={'DCE.jd2602':(9225,615,8775),'DCE.eg2605':(14145,943,3855),'DCE.jd2605':(17775,1185,225)}
    for row in result:
        assert (row['received_required_minutes'],row['complete_m15_proxy'],row['missing_minutes_proxy'])==expected_counts[row['actual_contract']]
        assert row['qualified_complete_m15']==0
    pending=pd.DataFrame(dict(eob=[t.isoformat() for t in sorted(day_minutes('2025-11-05'))],
        reference_present=True,candidate_present=False,comparison_status='NO_CANDIDATE_SOURCE'))
    pending.to_csv(a.output/'jd2605_overlap_pending.csv',index=False)
    (a.output/'jd2605_two_day_request.sql').write_text("SELECT instrument,trading_day,date,open,high,low,close,volume,open_interest\nFROM cn_future_bar1m\nWHERE instrument='jd2605.DCE' AND date >= '2025-11-04 00:00:00' AND date < '2025-11-06 00:00:00'\nORDER BY date;\n",encoding='utf8')
    check_lock();assert not list((ROOT/'data/cache/v71_score_claims').glob('*.json'))
    write(a.report,dict(status='READY_FOR_DATA',baseline_commit='1df718014532c0570f5167c540796653dd7ed432',
        full_field_new_minutes=sum(r['rows'] for r in checked),checked_ranges=checked,legs=result,
        union_manifest_sha256=digest(a.output/'union_manifest.json'),
        jd2605_overlap=dict(status='NO_CANDIDATE_SOURCE',expected_minutes=225,matched_minutes=None,
            comparison_performed=False,pending_table_sha256=digest(a.output/'jd2605_overlap_pending.csv')),
        existing_external_auth_or_raw_confirmed_by_user=False,strict_qualified_legs=0,
        historical_sessions='PENDING_AUTHORITY_AND_TEMPORARY_CHANGE_REVIEW',real_scoring_runs=0,
        orders_enabled=False,no_net_returns=True,
        source_canonical_sha256={str(x.relative_to(ROOT)).replace('\\','/'):canonical_digest(x) for x in
            [Path(__file__),ROOT/'tools/first_entry_v71/fetch_warmup_v712.py']}))
    print('FULL_FIELD_UNION_PASS 22800 new M1; proxy M15 JD615 EG943 JD2605 1185; qualified legs0; real score0')


if __name__=='__main__':main()
