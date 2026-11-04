"""Reproduce final comparison from immutable account ledgers, never rerun S0."""
from pathlib import Path
import argparse,shutil
import numpy as np
import pandas as pd
from backtest.forward_v3 import read,write,digest
from tools.historical_strategy.reproduce import canonical_digest
from tools.short_swing_v9.metrics import day_arrays,block_intervals

ROOT=Path(__file__).resolve().parents[2]


def ledger_check(trades,opened,summary):
    closed=float(trades.modeled_net_pnl.sum())
    unrealized=float(opened.unrealized_net_of_paid_entry_fee.sum()) if len(opened) else 0.
    error=summary['ending_equity']-1_000_000-closed-unrealized
    if abs(error)>.01:raise ValueError('account identity does not balance')
    if len(trades)!=summary['closed_trades']:raise ValueError('closed count mismatch')
    if not np.allclose(trades.gross_pnl-trades.modeled_fee,trades.modeled_net_pnl):raise ValueError('trade fees do not balance')
    if not np.allclose(trades.modeled_net_pnl/trades.initial_risk_money,trades.modeled_net_r):raise ValueError('R identity failed')
    return dict(cash_identity_error=error,closed_trades=len(trades),end_open_positions=len(opened))


def build(s0_root,ordinary_root,output):
    reg=read(ROOT/'tools/same_discipline_v10/registration.json')
    if output.exists():raise ValueError('fresh report directory required')
    output.mkdir(parents=True);table=[];by_product=[];uncertainty={};checks={};curves={};d_note={}
    for path,sha in reg['account_source_hashes'].items():
        if canonical_digest(ROOT/path)!=sha:raise ValueError('frozen account changed')
    dates=None
    for scenario in reg['scenarios']:
        source=s0_root/('v9_holding_'+scenario);ordinary=ordinary_root/('v10_ordinary_'+scenario)
        for name,sha in reg['reused_s0_file_hashes'][scenario].items():
            path=source/('summary.json' if name=='summary' else 'SHORT_S0_'+name+'.csv')
            if digest(path)!=sha:raise ValueError('reused S0 byte hash mismatch')
        old=read(source/'summary.json');new=read(ordinary/'summary.json')
        if new['registration_sha256']!=canonical_digest(ROOT/'tools/same_discipline_v10/registration.json'):raise ValueError('unregistered ordinary run')
        if new['account_source_hashes']!=reg['account_source_hashes']:raise ValueError('different accounts')
        arrays={};checks[scenario]={};curves[scenario]={}
        for arm,folder,prefix,s in [('S0_SHORT',source,'SHORT_S0',old['groups']['SHORT_S0']),
                ('ORDINARY_SHORT',ordinary,'ORDINARY_SHORT',new['groups']['ORDINARY_SHORT'])]:
            t=pd.read_csv(folder/(prefix+'_trades.csv'));o=pd.read_csv(folder/(prefix+'_opened.csv'));c=pd.read_csv(folder/(prefix+'_curve.csv'))
            checks[scenario][arm]=ledger_check(t,o,s)
            arm_dates=sorted(c.trading_date.unique().tolist())
            if dates is None:dates=arm_dates
            if dates!=arm_dates or len(dates)!=121:raise ValueError('full synchronized 121-day calendar required')
            arrays[arm]=day_arrays(t,c,dates);curves[scenario][arm]=c.groupby('trading_date').equity.last().reindex(dates)
            by=t.groupby('csymbol').agg(trades=('trade_id','size'),net_pnl=('modeled_net_pnl','sum'),mean_r=('modeled_net_r','mean'))
            top=by.net_pnl.idxmax();without=float(t.loc[t.csymbol!=top,'modeled_net_pnl'].sum())
            table.append(dict(scenario=scenario,arm=arm,closed_trades=len(t),open_positions=len(o),
                mean_r=s['mean_net_r'],closed_pnl=s['closed_net_pnl'],marked_pnl=s['total_marked_pnl'],
                maximum_m1_close_drawdown=s['maximum_m1_close_drawdown_pct'],longest_losing_streak=s['longest_losing_streak'],
                top_profit_product=top,closed_pnl_without_own_top_product=without,
                mean_r_without_own_top_product=float(t.loc[t.csymbol!=top,'modeled_net_r'].mean()),
                modeled_fees=s['modeled_fees'],costs_verified=False))
            for cs,row in by.iterrows():by_product.append(dict(scenario=scenario,arm=arm,csymbol=cs,**row.to_dict()))
            # Derived ledgers only; no raw M1 or credentials are published.
            for name in ('trades','opened','events','exit_attempts'):
                shutil.copyfile(folder/(prefix+'_'+name+'.csv'),output/(scenario+'_'+arm+'_'+name+'.csv'))
            if scenario=='GROSS' and arm=='S0_SHORT':
                d=t.loc[t.path=='D'];ex=d.loc[d.csymbol!='DCE.V']
                d_note=dict(status='DESCRIPTIVE_ONLY_NOT_D_ONLY_ACCOUNT',trades=len(d),mean_gross_r=float(d.gross_r.mean()),
                    without_pvc_trades=len(ex),without_pvc_mean_gross_r=float(ex.gross_r.mean()),new_replay=False)
        uncertainty[scenario]=block_intervals(arrays,dates,replicates=reg['bootstrap']['replicates'],seed=reg['bootstrap']['seed'])
    frame=pd.DataFrame(table);frame.to_csv(output/'decision_table.csv',index=False)
    products=pd.DataFrame(by_product);products.to_csv(output/'per_product.csv',index=False)
    differences=[]
    for scenario in reg['scenarios']:
        a=frame[(frame.scenario==scenario)&(frame.arm=='S0_SHORT')].iloc[0];b=frame[(frame.scenario==scenario)&(frame.arm=='ORDINARY_SHORT')].iloc[0]
        row=dict(scenario=scenario,mean_r_difference=float(a.mean_r-b.mean_r),closed_pnl_difference=float(a.closed_pnl-b.closed_pnl),
            marked_pnl_difference=float(a.marked_pnl-b.marked_pnl))
        for block in ('5','10'):row['difference_ci_'+block]=uncertainty[scenario]['blocks'][block]['first_minus_second']['mean_net_r_95ci']
        differences.append(row)
    stable=all(d['difference_ci_'+b][0]>0 for d in differences for b in ('5','10'))
    concentration_ok=all(frame.loc[(frame.arm=='S0_SHORT')&(frame.scenario.isin(['REFERENCE_1T','REFERENCE_2T'])),'closed_pnl_without_own_top_product']>0)
    status='CONTINUE_RESEARCH_REVIEW_ONLY' if stable and concentration_ok else 'ARCHIVE_COMPLEX_ENTRY_STOP_DEDICATED_PROCUREMENT'
    write(output/'comparison.json',dict(status=status,differences=differences,synchronized_score_dates=dates,
        stable_incremental_advantage=stable,positive_without_own_top_product=concentration_ok,
        old_window_diagnostic_only=True,independent_test_consumed=False,orders=False,new_data_requests=False,
        d_only_account_run=False,model_costs_verified=False,reused_s0_file_hashes=reg['reused_s0_file_hashes'],checks=checks,d_note=d_note))
    write(output/'uncertainty.json',uncertainty)
    # Daily closing display; headline drawdown remains full M1 close calculation.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(3,2,figsize=(13,10),sharex=True)
    display=[]
    for i,scenario in enumerate(reg['scenarios']):
        for arm,series in curves[scenario].items():
            axes[i,0].plot(pd.to_datetime(dates),series/1e6,label=arm)
            axes[i,1].plot(pd.to_datetime(dates),(series/series.cummax()-1)*100,label=arm)
            for date,equity in series.items():display.append(dict(scenario=scenario,arm=arm,trading_date=date,equity=equity))
        axes[i,0].set_title(scenario+' equity (million CNY)');axes[i,1].set_title('Daily close drawdown (%)')
        for ax in axes[i]:ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.autofmt_xdate();fig.tight_layout();fig.savefig(output/'equity_drawdown.png',dpi=150);plt.close(fig)
    pd.DataFrame(display).to_csv(output/'daily_equity.csv',index=False)
    write(output/'manifest.json',dict(registration_sha256=canonical_digest(ROOT/'tools/same_discipline_v10/registration.json'),
        files={p.name:digest(p) for p in sorted(output.iterdir()) if p.is_file()}))
    print(status,flush=True)
    print(frame[['scenario','arm','closed_trades','mean_r','maximum_m1_close_drawdown','closed_pnl_without_own_top_product']].to_string(index=False),flush=True)
    print(differences,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--s0-root',type=Path,required=True);p.add_argument('--ordinary-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();build(a.s0_root,a.ordinary_root,a.output)
