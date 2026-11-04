"""Read-only historical input/coverage lock; never computes trade outcomes."""
from pathlib import Path
import argparse,gzip,json,pickle
import numpy as np
import pandas as pd
from backtest.forward_v3 import read,write,digest,check_lock
from tools.historical_strategy.reproduce import canonical_digest,check_sources

ROOT=Path(__file__).resolve().parents[2]


def corrected_schedule(rows):
    dates=set(read(ROOT/'tools/full_trade_v8/calendar_corrections.json')['closed_night_start_dates'])
    frame=pd.DataFrame(rows);times=pd.DatetimeIndex(pd.to_datetime(frame.eob,utc=True)).tz_convert('Asia/Shanghai')
    closed=np.array([(t.date().isoformat() in dates and t.hour>=21) or
        ((t-pd.Timedelta(days=1)).date().isoformat() in dates and t.hour<3) for t in times])
    return times[~closed],np.array(frame.trading_date)[~closed],int(closed.sum())


def consecutive_eligibility(expected,actual):
    # Missing observations reset the count. Closed holiday sessions were removed independently.
    buckets=expected.ceil('15min');counts=pd.Series(expected.isin(actual).astype(int),index=buckets).groupby(level=0).agg(['sum','size'])
    run=0;out={}
    for t,r in counts.iterrows():
        run=run+1 if r['sum']==r['size']==15 else 0
        out[t.value]=run>=1200
    return out


def main():
    p=argparse.ArgumentParser();p.add_argument('--data-root',type=Path,required=True)
    p.add_argument('--replay-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists():raise ValueError('input lock already exists; use a new audited directory')
    check_lock();check_sources(read(ROOT/'reports/v6_reproduction_lock.json'))
    manifest=read(a.data_root/'manifest.json');legs={};coverage=[];input_hashes={};clocks={}
    for cs in sorted(manifest['products']):
        expected,days,removed=corrected_schedule(read(a.data_root/(cs+'_schedule.json')))
        ns=expected.as_unit('ns').asi8;daymap=dict(zip(ns,days));selection=read(a.data_root/(cs+'_selection.json'))
        bucket_days=dict(zip(expected.ceil('15min').as_unit('ns').asi8,days))
        for task in [r for r in manifest['tasks'] if r['csymbol']==cs]:
            symbol=task['symbol'];path=a.data_root/task['path'];raw_hash=digest(path)
            frame=pd.read_parquet(path).sort_values('eob').reset_index(drop=True)
            frame['eob']=pd.to_datetime(frame.eob,utc=True).dt.tz_convert('Asia/Shanghai')
            frame['bob']=pd.to_datetime(frame.bob,utc=True).dt.tz_convert('Asia/Shanghai')
            raw_ns=pd.DatetimeIndex(frame.eob).as_unit('ns').asi8;raw_set=set(raw_ns)
            assert len(set(raw_ns))==len(raw_ns) and (np.diff(raw_ns)>0).all()
            eligible=consecutive_eligibility(expected,pd.DatetimeIndex(frame.eob))
            replay=a.replay_root/(symbol+'.jsonl.gz');facts={}
            with gzip.open(replay,'rt',encoding='utf8') as stream:
                header=json.loads(next(stream));assert header['input_sha256']==raw_hash
                for line in stream:
                    row=json.loads(line);t=pd.Timestamp(row['decision_eob']).value
                    if t not in bucket_days:continue
                    core=row['groups']['S0'];entry=core.get('entry_evaluation')
                    allowed=bool(row['selected'] and eligible.get(t,False))
                    facts[t]=dict(core=core,i15=row['i15'],eligible=allowed,selected=row['selected'],complete=row['complete_m15'])
            # Only complete previous trading days may contribute to ADV. No current-day volume.
            raw_days=np.array([daymap.get(t,'UNKNOWN') for t in raw_ns])
            daily=pd.DataFrame(dict(day=raw_days,volume=frame.volume.to_numpy())).groupby('day').agg(volume=('volume','sum'),received=('volume','size'))
            expected_counts=pd.Series(days).value_counts();valid_daily={d:float(r.volume) for d,r in daily.iterrows() if r.received==expected_counts.get(d,-1)}
            ordered_days=sorted(set(days));adv={}
            for i,d in enumerate(ordered_days):
                previous=ordered_days[max(0,i-20):i]
                adv[d]=sum(valid_daily[x] for x in previous)/20 if len(previous)==20 and all(x in valid_daily for x in previous) else None
            values=frame[['open','high','low','close','volume']].to_numpy(dtype=float)
            valid=raw_ns>=ns.min();ix=np.searchsorted(ns,raw_ns);assert all(i<len(ns) and ns[i]==t for i,t in zip(ix[valid],raw_ns[valid]))
            # Source views are unadjusted actual contracts; closed-session bars are not generated.
            legs[symbol]=dict(cs=cs,symbol=symbol,frame=frame,ns=raw_ns,values=values,
                index={int(t):i for i,t in enumerate(raw_ns)},expected=ns,days=days,daymap=daymap,
                facts=facts,adv=adv,selection=selection,eligible=eligible)
            chosen=[t for t,r in facts.items() if r['eligible']]
            coverage.append(dict(csymbol=cs,symbol=symbol,raw_minutes=len(frame),eligible_m15=len(chosen),
                first_eligible=None if not chosen else pd.Timestamp(min(chosen),tz='UTC').tz_convert('Asia/Shanghai').isoformat(),
                last_eligible=None if not chosen else pd.Timestamp(max(chosen),tz='UTC').tz_convert('Asia/Shanghai').isoformat(),
                removed_closed_proxy_minutes=removed,missing_within_raw_span=int(sum(t not in raw_set for t in ns[(ns>=raw_ns.min())&(ns<=raw_ns.max())]))))
            input_hashes[str(path)]=raw_hash;input_hashes[str(replay)]=digest(replay)
            print('INPUT_LOCKED',symbol,'eligible',len(chosen),flush=True)
        clocks[cs]=dict(ns=ns,days=days)
    a.output.mkdir(parents=True)
    payload=dict(legs=legs,clocks=clocks,config=read(ROOT/'config/strategy.json'),fees=read(ROOT/'config/fees.json'))
    with (a.output/'prepared.pkl').open('wb') as stream:pickle.dump(payload,stream,protocol=5)
    write(a.output/'input_lock.json',dict(purpose='OLD_SAMPLE_TRADE_DIAGNOSTIC',registration_sha256=canonical_digest(ROOT/'tools/full_trade_v8/registration.json'),
        source_hashes=input_hashes,coverage=coverage,prepared_sha256=digest(a.output/'prepared.pkl'),no_pnl_read=True))
    write(ROOT/'reports/v8_coverage_lock.json',dict(status='LOCKED_BEFORE_OUTCOMES',coverage=coverage,
        input_lock_sha256=digest(a.output/'input_lock.json'),registration_sha256=canonical_digest(ROOT/'tools/full_trade_v8/registration.json')))
    print('V8_INPUT_LOCKED_BEFORE_OUTCOMES',flush=True)


if __name__=='__main__':main()
