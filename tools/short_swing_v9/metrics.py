"""Conditional ledger uncertainty; block length never changes a holding deadline."""
import numpy as np
import pandas as pd


def day_arrays(trades,curve,dates):
    if trades.empty:sums=pd.Series(dtype=float);counts=pd.Series(dtype=float)
    else:sums=trades.groupby('exit_day').modeled_net_r.sum();counts=trades.groupby('exit_day').size()
    eq=curve.groupby('trading_date').equity.last().reindex(dates).ffill().fillna(1_000_000.)
    return dict(r=sums.reindex(dates).fillna(0).to_numpy(),n=counts.reindex(dates).fillna(0).to_numpy(),
        pnl=np.diff(np.r_[1_000_000.,eq.to_numpy()]))


def block_intervals(arms,dates,replicates=10000,seed=20261104):
    n=len(dates);result={};names=list(arms)
    for block in (5,10):
        if n<block:result[str(block)]=dict(status='INSUFFICIENT_DAYS');continue
        rng=np.random.default_rng(seed)
        starts=rng.integers(0,n-block+1,size=(replicates,int(np.ceil(n/block))))
        indices=(starts[:,:,None]+np.arange(block)).reshape(replicates,-1)[:,:n];samples={};out={}
        for name,a in arms.items():
            total=a['n'][indices].sum(axis=1);sums=a['r'][indices].sum(axis=1)
            means=np.divide(sums,total,out=np.full(replicates,np.nan),where=total>0);samples[name]=means
            valid=means[np.isfinite(means)]
            out[name]=dict(mean_net_r_95ci=None if not len(valid) else np.quantile(valid,[.025,.975]).tolist(),
                marked_pnl_95ci=np.quantile(a['pnl'][indices].sum(axis=1),[.025,.975]).tolist(),
                zero_trade_draws=int((total==0).sum()))
        if len(names)==2:
            delta=samples[names[0]]-samples[names[1]];valid=delta[np.isfinite(delta)]
            out['first_minus_second']=dict(arms=names,mean_net_r_95ci=None if not len(valid) else np.quantile(valid,[.025,.975]).tolist())
        result[str(block)]=out
    return dict(blocks=result,calendar_days_including_zero_trades=n,replicates=replicates,seed=seed,
        interpretation='CONDITIONAL_REALIZED_LEDGER_UNCERTAINTY_NOT_REBALANCED_PRICE_PATHS')
