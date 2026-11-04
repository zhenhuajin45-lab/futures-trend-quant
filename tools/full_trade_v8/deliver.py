"""Summarize the frozen four runs; never selects or changes a trading rule."""
from pathlib import Path
import argparse,json,shutil
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from backtest.forward_v3 import read,write,digest,check_lock
from tools.historical_strategy.reproduce import canonical_digest

ROOT=Path(__file__).resolve().parents[2]
SCENARIOS=['GROSS','REFERENCE_1T','REFERENCE_2T','REFERENCE_3T']


def main():
    p=argparse.ArgumentParser();p.add_argument('--inputs',type=Path,required=True)
    p.add_argument('--gross',type=Path,required=True);p.add_argument('--reference-prefix',required=True)
    p.add_argument('--extended',type=Path,required=True);a=p.parse_args()
    table=[];monthly=[];concentration=[];audit=[];coverage=read(ROOT/'reports/v8_coverage_lock.json')['coverage']
    directories={s:(a.gross if s=='GROSS' else Path(a.reference_prefix+s)) for s in SCENARIOS}
    for scenario,directory in directories.items():
        summary=read(directory/'summary.json')
        assert summary['input_lock_sha256']==digest(a.inputs/'input_lock.json')
        for name in ['engine.py','run.py']:
            key='tools/full_trade_v8/'+name
            assert summary['code_canonical_sha256'][key]==canonical_digest(ROOT/key),(scenario,'CODE_MISMATCH')
        for group in ['S0','ORDINARY']:
            result=summary['groups'][group];trades=pd.read_csv(directory/(group+'_trades.csv'))
            opened=pd.read_csv(directory/(group+'_opened.csv'));events=pd.read_csv(directory/(group+'_events.csv'))
            curve=pd.read_csv(directory/(group+'_curve.csv'))
            assert len(trades)==result['closed_trades']
            assert (events.initial_risk+events.entry_debit<=events.product_risk_room_before+1e-6).all()
            assert (events.initial_risk+events.entry_debit<=events.portfolio_risk_room_before+1e-6).all()
            assert (events.margin_after<=events.equity_after*.8+1e-6).all()
            assert curve.open_positions.max()<=5
            assert (trades.entry_bob>=trades.signal_eob).all()
            assert not opened.unresolved_data_gap.any() and opened.mark_stale_calendar_minutes.eq(0).all()
            assert (trades.entry_price>0).all() and (trades.initial_risk_money>0).all()
            risk=(trades.entry_price-trades.stop).abs()*trades.lots
            assert risk.gt(0).all()
            assert abs(curve.equity.iloc[-1]-1_000_000-trades.modeled_net_pnl.sum()-opened.unrealized_net_of_paid_entry_fee.sum())<1e-5
            # Report is an independently financed rerun, not subtracting a cost constant.
            table.append(dict(scenario=scenario,group=group,**{k:v for k,v in result.items() if not isinstance(v,(dict,list))}))
            trades['exit_month']=trades.exit_day.str[:7]
            for month,rows in trades.groupby('exit_month'):
                monthly.append(dict(scenario=scenario,group=group,month=month,trades=len(rows),gross_pnl=rows.gross_pnl.sum(),
                    fees=rows.modeled_fee.sum(),net_pnl=rows.modeled_net_pnl.sum(),mean_gross_r=rows.gross_r.mean()))
            products=trades.groupby('csymbol').modeled_net_pnl.sum().sort_values(ascending=False)
            concentration.append(dict(scenario=scenario,group=group,top_product=products.index[0],
                top_product_closed_net=float(products.iloc[0]),closed_net_without_top_product=float(products.iloc[1:].sum()),
                top5_closed_net=float(trades.modeled_net_pnl.nlargest(5).sum()),
                closed_net_without_top5=float(trades.modeled_net_pnl.sum()-trades.modeled_net_pnl.nlargest(5).sum()),
                positive_products=int(products.gt(0).sum()),negative_products=int(products.lt(0).sum()),
                uncertain_entry_closed_net=float(trades.loc[trades.entry_bar_one_price|trades.entry_bar_zero_volume,'modeled_net_pnl'].sum()),
                gap_closed_net=float(trades.loc[trades.had_data_gap,'modeled_net_pnl'].sum())))
            audit.append(dict(scenario=scenario,group=group,cash_reconciled=True,max_positions=int(curve.open_positions.max()),
                next_open_after_signal=True,residual_daily_risk_caps_pass=True,margin_cap_pass=True,stale_end_marks=0,
                open_unresolved_gaps=0,first_entry=events.time.min(),last_entry=events.time.max(),
                observed_m1_equity_points=len(curve),maximum_realized_loss_r=float(trades.gross_r.min())))
    pd.DataFrame(table).to_csv(ROOT/'reports/v8_decision_table.csv',index=False)
    pd.DataFrame(monthly).to_csv(ROOT/'reports/v8_monthly.csv',index=False)
    write(ROOT/'reports/v8_concentration.json',concentration)
    cov=pd.DataFrame(coverage).groupby('csymbol').agg(eligible_m15=('eligible_m15','sum'),
        first_eligible=('first_eligible',lambda s:min(x for x in s if x is not None) if s.notna().any() else None),
        last_eligible=('last_eligible',lambda s:max(x for x in s if x is not None) if s.notna().any() else None)).reset_index()
    cov.to_csv(ROOT/'reports/v8_product_coverage.csv',index=False)
    # Export integrity metadata, never the licensed minute data.
    for source,name in [('tail_integrity_audit.json','v8_tail_integrity.json'),('fetch_audit.json','v8_tail_fetch.json'),('replay_audit.json','v8_tail_parity.json')]:
        shutil.copyfile(a.extended/source,ROOT/'reports'/name)
    lock=read(a.inputs/'input_lock.json')
    write(ROOT/'reports/v8_input_manifest.json',dict(input_lock_sha256=digest(a.inputs/'input_lock.json'),
        prepared_sha256=lock['prepared_sha256'],registration_sha256=lock['registration_sha256'],
        source_hashes={Path(k).name:v for k,v in lock['source_hashes'].items()},raw_prices_published=False))
    write(ROOT/'reports/v8_ledger_checks.json',dict(status='PASS_OFFLINE_LEDGER_ONLY',accounts=audit,
        formal_protocol_check=check_lock(),real_orders=False,simulation_orders=False,independent_validation=False,
        qualification='OLD_SAMPLE_PARTIAL_COVERAGE; PRICE_PROXY_AND_REFERENCE_COST_ASSUMPTIONS'))
    plt.rcParams['font.family']='DejaVu Sans'
    for scenario in ['GROSS','REFERENCE_1T']:
        fig,(ax,ddax)=plt.subplots(2,1,figsize=(11,6),sharex=True,gridspec_kw={'height_ratios':[2,1]})
        for group,color in [('S0','#146b56'),('ORDINARY','#aa5a24')]:
            c=pd.read_csv(directories[scenario]/(group+'_curve.csv'));c['t']=pd.to_datetime(c.eob,utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
            equity=c.equity.to_numpy();draw=equity/np.maximum.accumulate(np.r_[1_000_000.,equity])[1:]-1
            # M15 samples for equity; retain each interval's minimum drawdown.
            reduced=pd.DataFrame(dict(t=c.t,equity=equity,dd=draw)).set_index('t').resample('15min').agg({'equity':'last','dd':'min'}).dropna()
            ax.plot(reduced.index,reduced.equity/1_000_000,label=group,color=color,lw=1)
            ddax.plot(reduced.index,reduced.dd*100,label=group,color=color,lw=.8)
        ax.set(title='V8 '+scenario+' | old sample diagnostic',ylabel='Equity / initial 1m CNY');ax.legend();ax.grid(alpha=.2)
        ddax.set(ylabel='M1 close drawdown %',xlabel='Beijing time');ddax.grid(alpha=.2)
        fig.text(.01,.01,'M1 price proxies; 20% modeled margin; not independent validation. Intraminute drawdowns are unobserved.',fontsize=8)
        fig.tight_layout(rect=(0,.03,1,1));fig.savefig(ROOT/'reports'/('v8_'+scenario+'_equity_drawdown.png'),dpi=160)
        fig.savefig(ROOT/'reports'/('v8_'+scenario+'_equity_drawdown.svg'));plt.close(fig)
    print('V8_LEDGER_CHECKS_AND_ARTIFACTS_PASS',flush=True)


if __name__=='__main__':main()
