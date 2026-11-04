"""Run independently financed S0/ordinary accounts on the locked old input view."""
from pathlib import Path
import argparse,pickle
import numpy as np
import pandas as pd
from backtest.forward_v3 import read,write,digest,check_lock
from tools.historical_strategy.reproduce import canonical_digest
from tools.full_trade_v8.engine import Account

ROOT=Path(__file__).resolve().parents[2]


def summarize(result):
    trades=pd.DataFrame(result['trades']);curve=pd.DataFrame(result['curve']);opened=pd.DataFrame(result['opened'])
    eq=curve.equity.to_numpy();peaks=np.maximum.accumulate(np.r_[1_000_000.,eq])[1:]
    dd=eq-peaks;ddpct=dd/peaks
    run=longest=0
    for value in trades.get('modeled_net_pnl',[]):
        run=run+1 if value<0 else 0;longest=max(run,longest)
    def groups(key):
        return [] if trades.empty else trades.groupby(key).agg(trades=('gross_pnl','size'),gross_pnl=('gross_pnl','sum'),
            gross_r=('gross_r','sum'),fees=('modeled_fee','sum'),modeled_net_pnl=('modeled_net_pnl','sum')).reset_index().to_dict('records')
    top=trades.gross_pnl.nlargest(5).sum() if len(trades) else 0
    return dict(closed_trades=len(trades),open_positions=len(opened),gross_pnl=float(trades.gross_pnl.sum()) if len(trades) else 0,
        mean_gross_r=None if trades.empty else float(trades.gross_r.mean()),
        mean_net_r=None if trades.empty else float(trades.modeled_net_r.mean()),
        modeled_fees=float(trades.modeled_fee.sum()) if len(trades) else 0,
        closed_net_pnl=float(trades.modeled_net_pnl.sum()) if len(trades) else 0,
        ending_equity=float(eq[-1]),total_marked_pnl=float(eq[-1]-1_000_000),
        open_unrealized_pnl=float(opened.unrealized_pnl.sum()) if len(opened) else 0,
        maximum_m1_close_drawdown_currency=float(-dd.min()),maximum_m1_close_drawdown_pct=float(-ddpct.min()),
        longest_losing_streak=longest,win_rate=float(trades.modeled_net_pnl.gt(0).mean()) if len(trades) else None,
        gross_break_even_all_in_cost_per_trade=float(trades.gross_pnl.mean()) if len(trades) else None,
        one_tick_each_side_total_cost=float(trades.one_tick_each_side_cost.sum()) if len(trades) else 0,
        gross_break_even_average_ticks_per_side=float(trades.gross_pnl.sum()/trades.one_tick_each_side_cost.sum()) if len(trades) else None,
        gap_affected_closed_trades=int(trades.had_data_gap.sum()) if len(trades) else 0,
        double_touch_trades=int(trades.double_touch.sum()) if len(trades) else 0,
        unresolved_gap_positions=int(opened.unresolved_data_gap.sum()) if len(opened) else 0,
        top5_gross_contribution=float(top),gross_without_top5=float(trades.gross_pnl.sum()-top) if len(trades) else 0,
        per_product=groups('csymbol'),per_exit=groups('reason'),per_path=groups('path'),
        blockers=result['blocks'],dynamic_c_reselections=result['dynamic_c_reselections'])


def main():
    p=argparse.ArgumentParser();p.add_argument('--inputs',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--scenario',choices=['GROSS','REFERENCE_1T','REFERENCE_2T','REFERENCE_3T'],default='GROSS');a=p.parse_args()
    if a.output.exists():raise ValueError('output already exists; preserve previous audit outputs')
    lock=read(a.inputs/'input_lock.json')
    assert canonical_digest(ROOT/'tools/full_trade_v8/registration.json')==lock['registration_sha256']
    assert digest(a.inputs/'prepared.pkl')==lock['prepared_sha256'];check_lock()
    for path,expected in lock['source_hashes'].items():assert digest(Path(path))==expected,path
    with (a.inputs/'prepared.pkl').open('rb') as stream:data=pickle.load(stream)
    assert data['config']['execution']['execute_enabled'] is False
    a.output.mkdir(parents=True)
    costs=None if a.scenario=='GROSS' else read(ROOT/'tools/full_trade_v8/cost_reference.json')
    slip=0 if costs is None else int(a.scenario[-2]);summary={}
    for group in ['S0','ORDINARY']:
        result=Account(data,group,cost_model=costs,slip_ticks=slip).run();summary[group]=summarize(result)
        for name in ['trades','opened','events','curve']:
            frame=pd.DataFrame(result[name]);frame.to_csv(a.output/(group+'_'+name+'.csv'),index=False)
            if name!='curve':frame.to_csv(ROOT/'reports'/('v8_'+a.scenario+'_'+group+'_'+name+'.csv'),index=False)
            else:
                daily=frame.groupby('trading_date',sort=True).last().reset_index()
                daily['drawdown']=daily.equity/daily.equity.cummax()-1
                daily.to_csv(ROOT/'reports'/('v8_'+a.scenario+'_'+group+'_daily_equity.csv'),index=False)
        print('ACCOUNT_COMPLETED',a.scenario,group,summary[group]['closed_trades'],summary[group]['total_marked_pnl'],flush=True)
    report=dict(study='FULL_TRADE_OLD_SAMPLE_DIAGNOSTIC',scenario=a.scenario,groups=summary,
        input_lock_sha256=digest(a.inputs/'input_lock.json'),registration_canonical_sha256=lock['registration_sha256'],
        code_canonical_sha256={str(x.relative_to(ROOT)).replace('\\','/'):canonical_digest(x) for x in sorted((ROOT/'tools/full_trade_v8').glob('*.py'))},
        cost_reference_canonical_sha256=canonical_digest(ROOT/'tools/full_trade_v8/cost_reference.json'),
        real_orders=False,independent_validation=False,net_status='HYPOTHETICAL_REFERENCE_NOT_CERTIFIED_HISTORICAL_ACCOUNT',
        margin_status='20PCT_RESEARCH_ASSUMPTION',mark_basis='M1_CLOSE',v71_untouched=True)
    write(a.output/'summary.json',report);write(ROOT/'reports'/('v8_'+a.scenario+'_summary.json'),report)


if __name__=='__main__':main()
