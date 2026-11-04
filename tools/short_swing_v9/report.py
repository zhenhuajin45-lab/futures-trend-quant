"""Publish the one holding-policy diagnostic in a new namespace only."""
from pathlib import Path
import argparse,pickle,shutil
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from backtest.forward_v3 import read,write,digest
from .policy import HoldingCalendar
from .metrics import day_arrays,block_intervals

ROOT=Path(__file__).resolve().parents[2]


def holding_stats(trades,opened,data,calendar):
    t=trades.copy();hours=(pd.to_datetime(t.exit_time,utc=True)-pd.to_datetime(t.entry_bob,utc=True)).dt.total_seconds()/3600
    closure_events=[]
    for cs,c in data['clocks'].items():
        ns=c['ns'];gap=np.diff(ns)-60_000_000_000
        for i in np.flatnonzero(gap>=24*60*60_000_000_000):
            begin=pd.Timestamp(int(ns[i]),tz='UTC').tz_convert('Asia/Shanghai');end=pd.Timestamp(int(ns[i+1])-60_000_000_000,tz='UTC').tz_convert('Asia/Shanghai')
            kind='WEEKEND' if begin.weekday() in (4,5) and end.weekday()==0 and (end-begin).total_seconds()<90*3600 else 'LONG_HOLIDAY'
            closure_events.append((cs,begin,end,kind))
    allpos=t.to_dict('records')+opened.to_dict('records');cross=[]
    for r in allpos:
        start=pd.Timestamp(r['entry_bob']);finish=pd.Timestamp(r.get('exit_time') or r['last_mark_eob'])
        for cs,begin,end,kind in closure_events:
            if r['csymbol']==cs and start<=begin and finish>begin:
                cross.append(dict(trade_id=r['trade_id'],symbol=r['symbol'],entry_bob=r['entry_bob'],closure_start=begin.isoformat(),
                    resume=end.isoformat(),kind=kind))
    forced=t.reason.isin(['NEXT_TRADING_DAY_DEADLINE','PRE_LONG_CLOSURE_EXIT'])
    return dict(closed_trades=len(t),open_positions=len(opened),median_natural_hours=float(hours.median()),
        maximum_natural_hours=float(hours.max()),at_least_5_natural_days=int((hours>=120).sum()),at_least_10_natural_days=int((hours>=240).sum()),
        forced_exit_trades=int(forced.sum()),forced_by_reason=t.loc[forced].reason.value_counts().to_dict(),
        weekend_exposure_trades=len({r['trade_id'] for r in cross if r['kind']=='WEEKEND'}),
        long_holiday_exposure_trades=len({r['trade_id'] for r in cross if r['kind']=='LONG_HOLIDAY'}),
        all_closure_crossings=cross,execution_risk='MODEL_ONLY; NO_REAL_FLAT_CERTIFICATION')


def main():
    p=argparse.ArgumentParser();p.add_argument('--inputs',type=Path,required=True);a=p.parse_args()
    with (a.inputs/'prepared.pkl').open('rb') as f:data=pickle.load(f)
    target=ROOT/'reports/v9_holding';target.mkdir(parents=True,exist_ok=True)
    reg=read(ROOT/'tools/short_swing_v9/old_experiment_registration.json');calendar=HoldingCalendar(data['clocks'],**reg['holding_policy'])
    dates=sorted(set(d for c in data['clocks'].values() for d in c['days']));rows=[];holding={};uncertainty={};allcuts=[]
    for scenario in reg['scenarios']:
        source=ROOT/'data/cache'/('v9_holding_'+scenario);summary=read(source/'summary.json');arrays={};holding[scenario]={}
        shutil.copyfile(source/'summary.json',target/(scenario+'_summary.json'))
        baseline=read(ROOT.parent/'ftq-v3/reports'/('v8_'+scenario+'_summary.json'))['groups']['S0']
        for arm in ('FROZEN_S0','SHORT_S0'):
            s=summary['groups'][arm];trades=pd.read_csv(source/(arm+'_trades.csv'));opened=pd.read_csv(source/(arm+'_opened.csv'))
            curve=pd.read_csv(source/(arm+'_curve.csv'));events=pd.read_csv(source/(arm+'_events.csv'))
            assert abs(curve.equity.iloc[-1]-1_000_000-trades.modeled_net_pnl.sum()-opened.unrealized_net_of_paid_entry_fee.sum())<1e-5
            assert (events.margin_after<=events.equity_after*.8+1e-6).all()
            if arm=='FROZEN_S0':
                for key in ('closed_trades','total_marked_pnl','mean_net_r','maximum_m1_close_drawdown_pct'):assert s[key]==baseline[key]
            holding[scenario][arm]=holding_stats(trades,opened,data,calendar)
            arrays[arm]=day_arrays(trades,curve,dates)
            rows.append(dict({'scenario':scenario,'arm':arm,**{k:v for k,v in s.items() if not isinstance(v,(list,dict))},
                **{k:v for k,v in holding[scenario][arm].items() if not isinstance(v,(list,dict))}}))
            for name in ('trades','opened','events','exit_attempts'):shutil.copyfile(source/(arm+'_'+name+'.csv'),target/(scenario+'_'+arm+'_'+name+'.csv'))
            daily=curve.groupby('trading_date',sort=True).last().reset_index();daily['drawdown']=daily.equity/daily.equity.cummax()-1
            daily.to_csv(target/(scenario+'_'+arm+'_daily_equity.csv'),index=False)
        uncertainty[scenario]=block_intervals(arrays,dates)
        old=pd.read_csv(source/'FROZEN_S0_trades.csv');short=pd.read_csv(source/'SHORT_S0_trades.csv')
        joined=old.merge(short,on=['symbol','direction','entry_bob'],suffixes=('_old','_short'))
        for r in joined.to_dict('records'):
            if pd.Timestamp(r['exit_time_short'])>=pd.Timestamp(r['exit_time_old']):continue
            delta=r['gross_r_short']-r['gross_r_old']
            allcuts.append(dict(scenario=scenario,symbol=r['symbol'],direction=r['direction'],entry_bob=r['entry_bob'],
                old_exit=r['exit_time_old'],short_exit=r['exit_time_short'],old_reason=r['reason_old'],short_reason=r['reason_short'],
                old_lots=r['lots_old'],short_lots=r['lots_short'],old_gross_r=r['gross_r_old'],short_gross_r=r['gross_r_short'],gross_r_delta=delta,
                classification='LOSS_REDUCED' if r['gross_r_old']<0 and delta>0 else 'PROFIT_TRUNCATED' if r['gross_r_old']>0 and delta<0 else 'OTHER_EARLY_EXIT',
                interpretation='MATCHED_ENTRY_PRICE_PATH_COMPARISON_NOT_TOTAL_ACCOUNT_CAUSAL_EFFECT'))
    pd.DataFrame(rows).to_csv(target/'decision_table.csv',index=False)
    pd.DataFrame(allcuts).to_csv(target/'matched_early_exits.csv',index=False)
    write(target/'holding_exposure.json',holding);write(target/'block_uncertainty.json',uncertainty)
    cuts=pd.DataFrame(allcuts)
    write(target/'early_exit_summary.json',{s:dict(classifications=cuts.loc[cuts.scenario==s,'classification'].value_counts().to_dict(),
        matched_early_exits=int((cuts.scenario==s).sum()),sum_matched_gross_r_delta=float(cuts.loc[cuts.scenario==s,'gross_r_delta'].sum())) for s in reg['scenarios']})
    plt.rcParams['font.family']='DejaVu Sans';fig,(ax,dd)=plt.subplots(2,1,figsize=(11,6),sharex=True)
    for arm,color in [('FROZEN_S0','#146b56'),('SHORT_S0','#aa5a24')]:
        c=pd.read_csv(ROOT/'data/cache/v9_holding_REFERENCE_1T'/(arm+'_curve.csv'));t=pd.to_datetime(c.eob,utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
        peaks=np.maximum.accumulate(np.r_[1_000_000.,c.equity.to_numpy()])[1:]
        reduced=pd.DataFrame(dict(t=t,eq=c.equity/1_000_000,dd=100*(c.equity.to_numpy()/peaks-1))).set_index('t').resample('15min').agg({'eq':'last','dd':'min'}).dropna()
        ax.plot(reduced.index,reduced['eq'],label=arm,color=color,lw=1);dd.plot(reduced.index,reduced['dd'],label=arm,color=color,lw=.8)
    ax.set(title='V9 | fixed holding discipline | reference fees + 1 tick each side',ylabel='Equity / initial 1m CNY');ax.legend();ax.grid(alpha=.2)
    dd.set(ylabel='M1 close drawdown %',xlabel='Beijing time');dd.grid(alpha=.2)
    fig.text(.01,.01,'Old partial-coverage diagnostic. M1 proxy fills do not prove real pre-closure liquidation.',fontsize=8)
    fig.tight_layout(rect=(0,.03,1,1));fig.savefig(target/'equity_drawdown.png',dpi=160)
    svg=target/'equity_drawdown.svg';fig.savefig(svg);svg.write_text('\n'.join(x.rstrip() for x in svg.read_text(encoding='utf8').splitlines())+'\n',encoding='utf8')
    print('V9_ONE_COMPARISON_LEDGER_HOLDING_EXPOSURE_AND_UNCERTAINTY_COMPLETE',flush=True)


if __name__=='__main__':main()
