"""Read-only, descriptive V10 old-window diagnostics. See README.md."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import pickle
import sys

import numpy as np
import pandas as pd

NS = 60_000_000_000
ZONE = 'Asia/Shanghai'
LEDGER_HASH = '0ccd51fe99c367312e65906e3614f5806cdbe59ba9997a1230d7e105eed40258'
PACKAGE_HASH = '3709ad6bfc354efb1474ae59f71c0bdb40f0d54a210ce6c065ff902651a972ee'
LOCK_HASH = '6dfd477e14f03aa0dcd6d0b0dd69fd1f1b8683ea16fadfd44a1ab86e6ac0c97d'

SOURCE_HASHES = {'backtest/forward_v3.py': '7debc8bc2a6a0b9f5df1632dd19b5b38d71b6ff4476234e1e074ed21dc1a652a',
 'reports/v10_same_discipline/manifest.json': 'd52592608c7b17d6257b0f7b6e8c70524eac4ad630f0c9129764368c5f22cf6f',
 'reports/v4_calendar_basis.json': 'e005d31101435c36f7a272cd59bf68a419333e8c342e938ca806c59c7e6df4f3',
 'reports/v7_input_clock_hashes.json': 'ffa4ccbf60562dfc3c73bdf42583e918c19e3db31e3164a25ee7e005e9c59dc2',
 'reports/v8_input_manifest.json': '2d5c14eff64d1455b014408df0d9db3f86d83a566d2de0536e9c998f41c96c51',
 'tools/full_trade_v8/calendar_corrections.json': 'c1819d8589c68e2bfce74c86499ff8562555701f42efdbb4e2594958ff54820a',
 'tools/full_trade_v8/engine.py': '4eadb24760fcff06e103f7e5b839ecbb33e4be014706363f56350b93781d285e',
 'tools/full_trade_v8/prepare.py': '4fb0e4fd64da6205585c38be6a5dd86b2a69f895add49bdf8f9f5a827c5594d9',
 'tools/historical_strategy/prepare.py': '041f2c93ecf6d304e6de0d33469aa1c029547612aca01fba8ecdb2ab262f0b9c',
 'tools/short_swing_v9/policy.py': '0a756774f90ae30e03868138148bd659fa5348c56b1b4dec4dd88066b05f6e99'}

def sha(data):
    return hashlib.sha256(data).hexdigest()

def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def read_public(path, expected):
    raw = path.read_bytes()
    lf = raw.replace(b'\r\n', b'\n')
    if expected not in (sha(raw), sha(lf), sha(lf.replace(b'\n', b'\r\n'))):
        raise ValueError('Public input hash mismatch: ' + path.name)
    return pd.read_csv(io.BytesIO(raw))

def utc_ns(value):
    return pd.Timestamp(value).value

def public_clocks(repo):
    from tools.historical_strategy.prepare import make_schedule
    from tools.full_trade_v8.prepare import corrected_schedule
    report = repo/'reports'
    directory = report/'v10_same_discipline'
    manifest = json.loads((directory/'manifest.json').read_text())['files']
    name = 'daily_equity.csv'
    days = sorted(read_public(directory/name, manifest[name])['trading_date'].unique())
    if len(days) != 121 or days[0] != '2026-05-07' or days[-1] != '2026-11-02':
        raise ValueError('Wrong old window')
    locks = json.loads((report/'v7_input_clock_hashes.json').read_text())['v6_existing_clock_sha256']
    count = json.loads((report/'v4_calendar_basis.json').read_text())['expected_m1']
    calendars = {}
    for size in sorted(set(count.values())):
        intervals = [('09:00','10:15'), ('10:30','11:30'), ('13:30','15:00')]
        if size != 225:
            intervals.append(('21:00', {345:'23:00',555:'02:30'}[size]))
        sched = make_schedule(['2026-05-06'] + days, [dict(start=s,end=e) for s,e in intervals])
        rows = [dict(eob=t.isoformat(), trading_date=d) for t,d in sorted(sched.items())]
        payload = json.dumps(rows, indent=2, sort_keys=True) + '\n'
        hashes = {sha(payload.encode()),sha(payload.replace('\n','\r\n').encode())}
        times, dates, _ = corrected_schedule(rows)
        for cs, n in count.items():
            if n == size:
                if locks[cs+'_schedule.json'] not in hashes:
                    raise ValueError('Original schedule not reproduced: ' + cs)
                calendars[cs] = dict(ns=times.as_unit('ns').asi8,days=dates)
    return calendars

class Clock:
    def __init__(self, clock, policy):
        self.ends = clock['ns']
        self.starts = self.ends - NS
        self.dates = clock['days']
        self.deadlines = policy['deadlines']
        self.blackouts = policy['blackouts']
        self.local = pd.to_datetime(self.starts,utc=True).tz_convert(ZONE)
        self.slots = {}
        for i,t in enumerate(self.local):
            self.slots.setdefault((t.hour,t.minute),[]).append(i)

    def expiry(self, idx):
        bob = self.starts[idx]
        d1 = self.deadlines.get(self.dates[idx])
        for start,end in self.blackouts:
            if start <= bob < end:
                return start
            if start > bob:
                return start if d1 is None else min(start,d1)
        return d1

    def span(self,start,end):
        # Calendar minute opens in [start, end); never use number of observed quotes.
        return int(self.starts.searchsorted(end) - self.starts.searchsorted(start))

    def signature(self, idx, until):
        stop = self.starts.searchsorted(until)
        window = self.starts[idx:stop]
        if not len(window):
            return ()
        # Match clock-of-day, and lengths of each tradable block. Lunch/overnight never bridged.
        return tuple(np.diff(np.r_[0,np.flatnonzero(np.diff(window)!=NS)+1,len(window)]))

def measure_space(leg, clock, idx, signal, until, stop, direction):
    fact = leg['facts'].get(signal)
    if fact is None or not fact.get('eligible'):
        return np.nan, 'NO_SIGNAL_FACT'
    risk = (fact['core']['close'] - stop) * (1 if direction=='bull' else -1)
    if not np.isfinite(risk) or risk <= 0:
        return np.nan, 'INVALID_SIGNAL_R0'
    t = clock.local[idx]
    match = clock.signature(idx,until)
    if not match:
        return np.nan, 'EMPTY_WINDOW'
    chosen = []
    for i in reversed(clock.slots[(t.hour,t.minute)]):
        if i >= idx:
            continue
        end = clock.expiry(i)
        if end is None or end > signal or end <= clock.starts[i]:
            continue
        if clock.signature(i,end) == match:
            chosen.append((i,clock.starts.searchsorted(end)))
            if len(chosen)==20:
                break
    if len(chosen)!=20:
        return np.nan, 'FEWER_THAN_20_WINDOWS'
    spans = []
    for a,b in chosen:
        ids=[leg['index'].get(int(n)) for n in clock.ends[a:b]]
        if None in ids:
            return np.nan, 'MISSING_HISTORICAL_MINUTE'
        bars=leg['values'][ids]
        if not np.isfinite(bars[:,1:3]).all() or (bars[:,1]<bars[:,2]).any():
            return np.nan, 'INVALID_HISTORICAL_PRICE'
        spans.append(bars[:,1].max()-bars[:,2].min())
    return float(np.median(spans)/risk), 'OLD_WINDOW_EVENT_TIME_PROXY'

def first_touch(leg, clock, idx, end, direction, stop, target):
    # Ex-post explanatory label only. Ignore discretionary/trend exit for this price-path question.
    upper, lower = ('TARGET','STOP') if direction == 'bull' else ('STOP','TARGET')
    lo,hi=sorted((stop,target))
    last=clock.starts.searchsorted(end)
    for ns in clock.ends[idx:last]:
        index=leg['index'].get(int(ns))
        if index is None:
            return 'MISSING'
        op,h,l=leg['values'][index,:3]
        if not np.isfinite([op,h,l]).all() or not l<=op<=h:
            return 'MISSING'
        if op>=hi:
            return upper
        if op<=lo:
            return lower
        if h>=hi and l<=lo:
            return 'AMBIGUOUS'
        if h>=hi:
            return upper
        if l<=lo:
            return lower
    return 'NEITHER'

def bucket(series,k):
    valid=series.dropna()
    if valid.empty:
        return pd.Series(pd.NA,index=series.index,dtype='Int64'), []
    cuts=valid.quantile(np.arange(1,k)/k,interpolation='linear').to_numpy()
    # Identical observations stay together. Repeated boundaries can yield empty bins.
    result=pd.Series(pd.NA,index=series.index,dtype='Int64')
    result.loc[valid.index]=np.searchsorted(cuts,valid,side='left')+1
    return result,cuts.tolist()

def stats(frame):
    n=len(frame)
    return dict(n=n,mean_gross_r=frame.gross_r.mean(),mean_held_minutes=frame.hold_minutes.mean(),
                negative_share=(frame.gross_r<0).mean() if n else np.nan,
                products=frame.csymbol.nunique(), dates=frame.entry_day.nunique(),
                **{('path_'+code.lower()):int((frame.price_path==code).sum()) for code in
                   ['TARGET','STOP','AMBIGUOUS','MISSING','NEITHER','NOT_COMPUTABLE']})

def grid(frame,k):
    common=frame.dropna(subset=['remaining_minutes','range_ratio']).copy()
    common['t'],ct=bucket(common.remaining_minutes,k)
    common['s'],cs=bucket(common.range_ratio,k)
    rows=[dict(t=i,s=j,**stats(common[(common.t==i)&(common.s==j)]))
          for i in range(1,k+1) for j in range(1,k+1)]
    corners=[]
    for product,f in common.groupby('csymbol'):
        lo=f[(f.t==1)&(f.s==1)].gross_r
        hi=f[(f.t==k)&(f.s==k)].gross_r
        corners.append(dict(product=product,n_low=len(lo),n_high=len(hi),
                            high_minus_low=hi.mean()-lo.mean()))
    return rows,ct,cs,pd.DataFrame(corners,columns=['product','n_low','n_high','high_minus_low'])

def run(a):
    repo=a.repo.resolve()
    for name,expected in SOURCE_HASHES.items():
        if sha((repo/name).read_bytes().replace(b'\r\n',b'\n'))!=expected:
            raise ValueError('Wrong frozen V10 source: '+name)
    sys.path.insert(0,str(repo))
    from tools.short_swing_v9.policy import HoldingCalendar
    root=repo/'reports/v10_same_discipline'
    trades=read_public(root/'GROSS_ORDINARY_SHORT_trades.csv',LEDGER_HASH)
    manifest=json.loads((root/'manifest.json').read_text())['files']
    open_name='GROSS_ORDINARY_SHORT_opened.csv'
    opened=read_public(root/open_name,manifest[open_name])
    if (len(trades)!=631 or trades.trade_id.duplicated().any() or
        set(trades.group)!={'ORDINARY'} or set(trades.trade_id)&set(opened.trade_id) or len(opened)!=5):
        raise ValueError('V10 cohort mismatch')
    clocks=public_clocks(repo)
    policies=HoldingCalendar(clocks).products
    clock_by_cs={cs:Clock(clock,policies[cs]) for cs,clock in clocks.items()}
    data=None
    if a.prepared:
        lock=a.prepared.parent/'input_lock.json'
        if file_hash(lock)!=LOCK_HASH or file_hash(a.prepared)!=PACKAGE_HASH:
            raise ValueError('Not the frozen V10 old input; do not load')
        with a.prepared.open('rb') as f:
            data=pickle.load(f)
        for cs,c in clocks.items():
            if not (np.array_equal(c['ns'],data['clocks'][cs]['ns']) and
                    np.array_equal(c['days'],data['clocks'][cs]['days'])):
                raise ValueError('Prepared clock mismatches public reconstruction')
    rows=[]
    for r in trades.itertuples():
        clock=clock_by_cs[r.csymbol]; ent=utc_ns(r.entry_bob); sig=utc_ns(r.signal_eob)
        # Market-clock projection only; the realized entry timestamp serves solely as an audit.
        idx=int(clock.ends.searchsorted(sig,side='right'))
        if idx==len(clock.ends) or clock.starts[idx]!=ent or clock.dates[idx]!=r.entry_day:
            raise ValueError('Legal entry mismatch: '+str(r.trade_id))
        end=clock.expiry(idx)
        remaining=clock.span(ent,end) if end is not None and end>=ent else np.nan
        ratio=np.nan; reason='DEADLINE_UNKNOWN' if not np.isfinite(remaining) else 'NO_LOCAL_PRICE_PACKAGE'
        path_label='NOT_COMPUTABLE'
        if data is not None and np.isfinite(remaining):
            path_label=first_touch(data['legs'][r.symbol],clock,idx,end,r.direction,r.stop,r.target)
            ratio,reason=measure_space(data['legs'][r.symbol],clock,idx,sig,end,r.stop,r.direction)
        rows.append(dict(trade_id=r.trade_id,csymbol=r.csymbol,symbol=r.symbol,entry_day=r.entry_day,
                         gross_r=r.gross_r,remaining_minutes=remaining,hold_minutes=clock.span(ent,utc_ns(r.exit_time)),
                         range_ratio=ratio,space_status=reason,price_path=path_label,exit_reason=r.reason))
    frame=pd.DataFrame(rows)
    summary=dict(sample='V10_GROSS_ORDINARY_SHORT', scope='closed_executed_trades_only',closed=len(frame),
                 open_excluded=len(opened),clock_hashes_matched=len(clocks),gross_r=frame.gross_r.mean(),
                 negative_count=int((frame.gross_r<0).sum()),known_time=int(frame.remaining_minutes.notna().sum()),
                 known_space=int(frame.range_ratio.notna().sum()),space_status=frame.space_status.value_counts().to_dict())
    frame['time_bin'],summary['time_only_cut_points']=bucket(frame.remaining_minutes,3)
    time_table=pd.DataFrame([dict(t=i,**stats(frame[frame.time_bin==i])) for i in range(1,4)])
    output=a.out.resolve()
    if output.exists():
        raise ValueError('Fresh output directory required')
    import tempfile
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        stage=Path(temp)/'results';stage.mkdir()
        frame.to_csv(stage/'audit.csv',index=False)
        time_table.to_csv(stage/'time_only.csv',index=False)
        composition=frame.groupby(['csymbol','time_bin']).gross_r.agg(['count','mean'])
        composition.to_csv(stage/'time_by_product.csv')
        for k in [3,2]:
            cells,ct,cs,corners=grid(frame,k)
            summary[str(k)+'x'+str(k)]=dict(time_cuts=ct,space_cuts=cs,status='DESCRIPTIVE' if cs else 'NOT_COMPUTABLE')
            pd.DataFrame(cells).to_csv(stage/f'grid_{k}.csv',index=False)
            corners.to_csv(stage/f'within_product_{k}.csv',index=False)
        (stage/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False,allow_nan=False),encoding='utf8')
        stage.rename(output)
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--repo',required=True,type=Path)
    p.add_argument('--prepared',type=Path,help='Original frozen local V10 prepared.pkl and adjacent input_lock.json')
    p.add_argument('--out',required=True,type=Path)
    run(p.parse_args())
