"""Official independent entry: qualify, consume once, replay, then judge once."""
from pathlib import Path
import argparse,pickle
import pandas as pd
from backtest.forward_v3 import read,write,digest
from tools.historical_strategy.reproduce import canonical_digest
from .independent import execute,ROOT,SCENARIOS
from .metrics import day_arrays,block_intervals
from .cost_evidence import decision_cost_status


def complete_scoring_dates(data,dates):
    complete=set(dates)
    for cs,clock in data['clocks'].items():
        selected_legs=[l for l in data['legs'].values() if l['cs']==cs]
        if not selected_legs:return []
        mapping=selected_legs[0]['selection'];by_day={d:[] for d in dates}
        for t,d in zip(clock['ns'],clock['days']):
            if d in by_day:by_day[d].append(int(t))
        for day,times in by_day.items():
            leg=data['legs'].get(mapping.get(day));buckets={(t+900_000_000_000-1)//900_000_000_000*900_000_000_000 for t in times}
            if not times or leg is None or not all(t in leg['index'] for t in times) or not buckets<=set(leg['facts']):
                complete.discard(day)
    return sorted(complete)


def judgment(summary,uncertainty,concentration,reg,cost_assessment,observed_complete_days=0):
    checks={};days=observed_complete_days;capacity=reg['capacity']
    checks['minimum_days']=days>=capacity['minimum_score_days']
    for scenario in ('REFERENCE_1T','REFERENCE_2T'):
        for group in ('S0','ORDINARY'):
            checks[scenario+'_'+group+'_capacity']=summary[scenario][group]['closed_trades']>=capacity['minimum_closed_trades_each_arm']
        s=summary[scenario]['S0'];checks[scenario+'_mean_net_r']=(s['mean_net_r'] is not None and s['mean_net_r']>0)
        checks[scenario+'_marked_pnl']=s['total_marked_pnl']>0
        checks[scenario+'_drawdown']=s['maximum_m1_close_drawdown_pct']<=reg['maximum_m1_close_drawdown']
        for block in ('5','10'):
            ci=uncertainty[scenario]['blocks'][block].get('S0',{}).get('mean_net_r_95ci')
            checks[scenario+'_ci'+block]=(ci is not None and ci[0]>0)
        checks[scenario+'_without_top_product']=concentration[scenario]['S0']['without_top_product']>0
        checks[scenario+'_without_top5']=concentration[scenario]['S0']['without_top5']>0
    cost_status=decision_cost_status(cost_assessment)
    if cost_status in ('INSUFFICIENT_COST_MARGIN','COST_MODEL_REPLAY_REQUIRED'):status=cost_status
    elif not all(v for k,v in checks.items() if k.endswith('_capacity') or k=='minimum_days'):
        status='INSUFFICIENT_EVIDENCE_CAPACITY'
    elif cost_status!='VERIFIED_WITHIN_BUDGET':status='ECONOMICS_PENDING_COST_VALIDATION'
    elif all(checks.values()):status='ECONOMIC_CRITERIA_PASS_EXECUTION_REVIEW_STILL_REQUIRED'
    else:
        clearly_negative=False;conflict=False
        for scenario in ('REFERENCE_1T','REFERENCE_2T'):
            s=summary[scenario]['S0'];mean=s['mean_net_r'];money=s['total_marked_pnl']
            if mean is not None and mean*money<0:conflict=True
            intervals=[uncertainty[scenario]['blocks'][b].get('S0',{}).get('mean_net_r_95ci') for b in ('5','10')]
            if mean is not None and mean<0 and money<0 and all(ci is not None and ci[1]<0 for ci in intervals):clearly_negative=True
        status='END_CURRENT_VERSION_SIM_PROMOTION' if clearly_negative and not conflict else 'INSUFFICIENT_EVIDENCE_NO_SIM'
    return dict(status=status,checks=checks,observed_complete_days=days,
        cost_assessment=cost_assessment,costs_certified=cost_status=='VERIFIED_WITHIN_BUDGET',simulation_orders_enabled=False)


def run(protocol,inputs,output,study_dir=None,synthetic=False):
    protocol,inputs,output=Path(protocol),Path(inputs),Path(output);reg=read(protocol)
    if not synthetic:
        freeze=read(ROOT/'tools/short_swing_v9/independent_freeze.json')
        if freeze['protocol_sha256']!=canonical_digest(protocol):raise ValueError('not frozen independent protocol')
        for path,sha in reg['decision_hashes'].items():
            if canonical_digest(ROOT/path)!=sha:raise ValueError('independent decision code changed')
        if reg['status']!='FROZEN_FOR_QUALIFIED_DATA':raise ValueError('SOURCE_BLOCKED; no real replay attempted')
        lock=read(inputs/'input_lock.json')
        if digest(inputs/'prepared.pkl')!=lock['prepared_sha256']:raise ValueError('prepared input hash mismatch')
        with (inputs/'prepared.pkl').open('rb') as f:data=pickle.load(f)
        for clock in data['clocks'].values():
            after={d for d in clock['days'] if d>reg['score_trade_dates'][-1]}
            if len(after)<2:raise ValueError('two future calendar trading days required; no future prices requested')
        # execute() validates the numeric cost contract before consuming the token;
        # its final result carries execution coverage, not caller-provided booleans.
    result=execute(protocol,inputs,output,study_dir,synthetic)
    if synthetic:
        write(output/'decision.json',dict(status='SYNTHETIC_ONLY_NO_REAL_VERDICT',unique_real_test_consumed=False));return result
    uncertainty={};concentration={};complete_dates=set(complete_scoring_dates(data,reg['score_trade_dates']))
    for scenario in SCENARIOS:
        arrays={};concentration[scenario]={}
        for group in ('S0','ORDINARY'):
            trades=pd.read_csv(output/(scenario+'_'+group+'_trades.csv'));curve=pd.read_csv(output/(scenario+'_'+group+'_curve.csv'))
            if not trades.empty:
                for row in trades.loc[trades.had_data_gap].to_dict('records'):
                    first=data['legs'][row['symbol']]['daymap'].get(pd.Timestamp(row['first_gap_eob']).value)
                    if first is None:complete_dates.clear()
                    else:complete_dates={d for d in complete_dates if not first<=d<=row['exit_day']}
            if result['groups'][scenario][group]['unresolved_gap_positions']:complete_dates.clear()
            arrays[group]=day_arrays(trades,curve,reg['score_trade_dates'])
            if trades.empty:concentration[scenario][group]=dict(without_top_product=0.,without_top5=0.)
            else:
                total=float(trades.modeled_net_pnl.sum());by=trades.groupby('csymbol').modeled_net_pnl.sum()
                concentration[scenario][group]=dict(without_top_product=total-float(by.max()),without_top5=total-float(trades.modeled_net_pnl.nlargest(5).sum()))
        uncertainty[scenario]=block_intervals(arrays,reg['score_trade_dates'])
    decision=judgment(result['groups'],uncertainty,concentration,reg,result['cost_assessment'],len(complete_dates))
    decision['complete_scoring_trade_dates']=sorted(complete_dates)
    write(output/'uncertainty.json',uncertainty);write(output/'concentration.json',concentration);write(output/'decision.json',decision)
    token=Path(study_dir)/'independent_consumed.json';stamp=read(token);stamp['state']='COMPLETE_TEST_CONSUMED'
    temporary=token.with_suffix('.tmp');write(temporary,stamp);temporary.replace(token)
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--protocol',type=Path,required=True);p.add_argument('--inputs',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--study-dir',type=Path);p.add_argument('--synthetic',action='store_true');a=p.parse_args()
    result=run(a.protocol,a.inputs,a.output,a.study_dir,a.synthetic);print(result['status'])


if __name__=='__main__':main()
