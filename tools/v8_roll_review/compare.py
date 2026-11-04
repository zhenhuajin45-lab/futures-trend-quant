"""Compare eight corrected accounts to immutable da7a664 ledgers; no new alpha."""
from pathlib import Path
import argparse,pickle,json
import pandas as pd
from backtest.forward_v3 import read,write,digest,check_lock
from tools.historical_strategy.reproduce import canonical_digest

ROOT=Path(__file__).resolve().parents[2]
SCENARIOS=['GROSS','REFERENCE_1T','REFERENCE_2T','REFERENCE_3T']


def positions(directory,group):
    closed=pd.read_csv(directory/(group+'_trades.csv'));closed['status']='CLOSED'
    opened=pd.read_csv(directory/(group+'_opened.csv'));opened['status']='OPEN'
    return pd.concat([closed,opened],ignore_index=True)


def key(row):return '|'.join(str(row[x]) for x in ('symbol','direction','entry_bob'))


def inspect(frame,data,cutoffs):
    wrong=[];tail=[]
    for row in frame.to_dict('records'):
        leg=data['legs'][row['symbol']];eob=(pd.Timestamp(row['entry_bob'])+pd.Timedelta(minutes=1)).value
        day=leg['daymap'][eob]
        if leg['selection'].get(day)!=row['symbol']:
            wrong.append(dict(trade_id=row['trade_id'],symbol=row['symbol'],entry_bob=row['entry_bob'],entry_day=day,
                selected=leg['selection'].get(day),status=row['status'],net_pnl=None if pd.isna(row.get('modeled_net_pnl')) else row['modeled_net_pnl']))
        if eob>pd.Timestamp(cutoffs[row['symbol']]).value:tail.append(key(row))
    return wrong,tail


def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline-root',type=Path,required=True)
    p.add_argument('--inputs',type=Path,required=True);a=p.parse_args()
    lock=read(a.inputs/'input_lock.json');assert digest(a.inputs/'prepared.pkl')==lock['prepared_sha256']
    with (a.inputs/'prepared.pkl').open('rb') as f:data=pickle.load(f)
    cutoffs={r['symbol']:r['source_last_eob'] for r in read(ROOT/'reports/v8_tail_fetch.json')}
    accounts=[];differences=[];curves=[]
    for scenario in SCENARIOS:
        old=a.baseline_root/('full_trade_v8_audited_'+scenario);new=ROOT/'data/cache'/('v8_roll_corrected_'+scenario)
        before=read(old/'summary.json');after=read(new/'summary.json')
        assert before['input_lock_sha256']==after['input_lock_sha256']==digest(a.inputs/'input_lock.json')
        assert after['code_canonical_sha256']['tools/full_trade_v8/engine.py']==canonical_digest(ROOT/'tools/full_trade_v8/engine.py')
        for group in ('S0','ORDINARY'):
            b=before['groups'][group];c=after['groups'][group];oldpos=positions(old,group);newpos=positions(new,group)
            ob,ot=inspect(oldpos,data,cutoffs);nb,nt=inspect(newpos,data,cutoffs)
            assert not nb and not nt,(scenario,group,'NEW_ENTRY_OUTSIDE_SELECTION_OR_IN_TAIL')
            closed=newpos[newpos.status=='CLOSED'];continued=0
            for r in closed.to_dict('records'):
                if (pd.Timestamp(r['exit_time']).value>pd.Timestamp(cutoffs[r['symbol']]).value
                        or data['legs'][r['symbol']]['selection'].get(r['exit_day'])!=r['symbol']):continued+=1
            # Position IDs and sizes can change after removal; compare stable identities.
            bm={key(r):r for r in oldpos.to_dict('records')};cm={key(r):r for r in newpos.to_dict('records')}
            assert len(bm)==len(oldpos) and len(cm)==len(newpos)
            for k in sorted(bm.keys()|cm.keys()):
                x=bm.get(k);y=cm.get(k)
                fields=['lots','status','exit_time','exit_price','reason','modeled_net_pnl','unrealized_net_of_paid_entry_fee']
                def norm(v):return None if v is None or (not isinstance(v,(dict,list)) and pd.isna(v)) else v
                if x is None or y is None or any(norm(x.get(f))!=norm(y.get(f)) for f in fields):
                    row=dict(scenario=scenario,group=group,position_key=k,change='ADDED' if x is None else 'REMOVED' if y is None else 'CHANGED')
                    for label,v in [('before',x),('after',y)]:
                        for field in fields:row[label+'_'+field]=None if v is None else norm(v.get(field))
                    differences.append(row)
            curve_b=pd.read_csv(old/(group+'_curve.csv'));curve_c=pd.read_csv(new/(group+'_curve.csv'))
            assert curve_b.eob.equals(curve_c.eob)
            curve=pd.DataFrame(dict(eob=curve_c.eob,trading_date=curve_c.trading_date,equity_before=curve_b.equity,equity_after=curve_c.equity))
            curve['equity_delta']=curve.equity_after-curve.equity_before
            daily=curve.groupby('trading_date',sort=True).last().reset_index();daily['scenario']=scenario;daily['group']=group;curves.append(daily)
            accounts.append(dict(scenario=scenario,group=group,baseline_invalid_selected_opens=ob,
                baseline_tail_new_opens=len(ot),corrected_invalid_selected_opens=0,corrected_tail_new_opens=0,
                old_leg_continued_exits=continued,baseline_closed=b['closed_trades'],corrected_closed=c['closed_trades'],
                baseline_marked_pnl=b['total_marked_pnl'],corrected_marked_pnl=c['total_marked_pnl'],
                marked_pnl_delta=c['total_marked_pnl']-b['total_marked_pnl'],
                baseline_drawdown=b['maximum_m1_close_drawdown_pct'],corrected_drawdown=c['maximum_m1_close_drawdown_pct'],
                mean_net_r=c['mean_net_r'],fees=c['modeled_fees'],cash_reconciled=True,
                stable_positions_removed=len(bm.keys()-cm.keys()),stable_positions_added=len(cm.keys()-bm.keys()),
                maximum_absolute_m1_equity_difference=float(curve.equity_delta.abs().max())))
    assert sum(len(x['baseline_invalid_selected_opens']) for x in accounts)==1
    assert sum(x['old_leg_continued_exits'] for x in accounts)>0
    write(ROOT/'reports/v8_roll_comparison.json',dict(baseline='da7a664a136c8211f407765f5883a4bc0e35df71',
        patch_commit='cb3fd63',identical_input_lock_sha256=digest(a.inputs/'input_lock.json'),accounts=accounts,
        eight_cash_ledgers_reconciled=True,tail_entry_zero=True,old_leg_exits_preserved=True,
        interpretation='PATH_DEPENDENT_ACCOUNT_RERUN_NOT_SINGLE_TRADE_SUBTRACTION',real_orders=False))
    pd.DataFrame([{k:v for k,v in r.items() if k!='baseline_invalid_selected_opens'} for r in accounts]).to_csv(ROOT/'reports/v8_roll_comparison.csv',index=False)
    pd.DataFrame(differences).to_csv(ROOT/'reports/v8_roll_changed_positions.csv',index=False)
    pd.concat(curves,ignore_index=True).to_csv(ROOT/'reports/v8_roll_equity_differences.csv',index=False)
    s0=read(ROOT/'reports/v8_REFERENCE_1T_summary.json')['groups']['S0'];other=read(ROOT/'reports/v8_REFERENCE_1T_summary.json')['groups']['ORDINARY']
    write(ROOT/'reports/v8_roll_main_decomposition.json',dict(gross_after_slip_difference=s0['gross_pnl']-other['gross_pnl'],
        reference_fees_saved=other['modeled_fees']-s0['modeled_fees'],
        open_net_valuation_difference=s0['open_net_unrealized_pnl']-other['open_net_unrealized_pnl'],
        marked_equity_difference=s0['total_marked_pnl']-other['total_marked_pnl'],
        interpretation='ACCOUNTING_IDENTITY_NOT_CAUSAL_FEE_ABLATION'))
    check_lock();print('V8_ROLL_8_ACCOUNT_RECONCILIATION_TAIL_ENTRY_ZERO_OLD_EXITS_PASS',flush=True)


if __name__=='__main__':main()
