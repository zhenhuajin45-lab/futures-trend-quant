"""Isolated independent runner: warmup facts only, no legacy reports or account carry."""
from pathlib import Path
from copy import deepcopy
from collections import defaultdict
import argparse,hashlib,json,pickle,tempfile
import numpy as np
import pandas as pd
from backtest.forward_v3 import read,write,digest,check_lock
from tools.historical_strategy.reproduce import canonical_digest
from tools.full_trade_v8.run import summarize
from .account import ShortSwingAccount

ROOT=Path(__file__).resolve().parents[2]
SCENARIOS=('GROSS','REFERENCE_1T','REFERENCE_2T','REFERENCE_3T')
REQUIRED_ENGINE_FILES={'tools/full_trade_v8/engine.py','tools/short_swing_v9/account.py',
    'tools/short_swing_v9/policy.py','tools/short_swing_v9/independent.py',
    'config/fees.json','tools/full_trade_v8/cost_reference.json'}


def scoring_view(data,registration,synthetic=False):
    dates=registration['score_trade_dates'];universe=sorted(data['clocks'])
    if not dates or dates!=sorted(set(dates)):raise ValueError('invalid registered trade dates')
    if not synthetic and len(dates)!=82:raise ValueError('fixed independent date set changed')
    minimum=registration['source_admission']
    if len(universe)<minimum['minimum_source_qualified_products'] or not set(universe)<=set(registration['candidate_universe']):
        raise ValueError('unqualified or undeclared subset')
    if registration['maximum_m1_close_drawdown']!=.15 and not synthetic:raise ValueError('15pct budget changed')
    if data['config']['execution']['execute_enabled'] is not False:raise ValueError('execution must remain disabled')
    if data['config']['backtest']['initial_cash']!=1_000_000:raise ValueError('initial capital mismatch')
    view=dict(data,calendar_clocks=data['clocks'],clocks={},legs={});admission=[]
    for cs,clock in data['clocks'].items():
        times=np.asarray(clock['ns'],dtype=np.int64);days=np.asarray(clock['days'])
        mask=np.isin(days,dates);selected=times[mask]
        if set(days[mask])!=set(dates) or not len(selected):raise ValueError('scoring calendar coverage absent')
        if len(set(times))!=len(times) or (np.diff(times)<=0).any():raise ValueError('calendar duplicate/order')
        view['clocks'][cs]=dict(ns=selected,days=days[mask].tolist())
        numerator=eligible=denominator=expected_facts=actual_facts=0
        buckets=pd.to_datetime(times,utc=True).ceil('15min').asi8
        expected_buckets=defaultdict(set)
        for t,b in zip(times,buckets):expected_buckets[int(b)].add(int(t))
        if any(len(members)!=15 for members in expected_buckets.values()):
            raise ValueError('independent calendar has incomplete M15 buckets')
        product_legs=[l for l in data['legs'].values() if l['cs']==cs]
        if not product_legs:raise ValueError('no actual-contract raw data')
        mapping=product_legs[0]['selection']
        if any(l['selection']!=mapping for l in product_legs):raise ValueError('mapping disagreement')
        for day in dates:
            symbol=mapping.get(day)
            if symbol not in data['legs'] or data['legs'][symbol]['cs']!=cs:raise ValueError('missing actual selected leg')
        for leg in product_legs:
            if not len(leg['values']) or not leg['facts']:raise ValueError('empty raw or replay coverage')
            raw=set(leg['index']);first=[];run=0;ready={}
            for end,members in sorted(expected_buckets.items()):
                run=run+1 if len(members)==15 and members<=raw else 0
                ready[end]=run>=minimum['strict_complete_same_leg_prewarm_m15']
            chosen=[t for t,d in zip(times,days) if d in dates and mapping.get(d)==leg['symbol']]
            if not chosen:continue
            first_anchor=min(chosen)-60_000_000_000
            preceding=[t for t in ready if t<=first_anchor]
            if not preceding or not ready[max(preceding)]:raise ValueError('1200 complete same-leg prewarm unavailable')
            denominator+=len(chosen);numerator+=sum(t in raw for t in chosen)
            score_buckets=sorted(set(pd.to_datetime(chosen,utc=True).ceil('15min').asi8))
            for t in score_buckets:
                expected_facts+=1
                row=leg['facts'].get(int(t))
                if row is None:continue
                actual_facts+=1
                if row.get('eligible') and ready.get(int(t),False):eligible+=1
            view['legs'][leg['symbol']]=leg
        if numerator/max(1,denominator)<minimum['selected_expected_minute_coverage_per_product_minimum']:
            raise ValueError('raw scoring minute coverage below threshold')
        if expected_facts==0 or actual_facts<expected_facts*.95:raise ValueError('strategy replay coverage absent/incomplete')
        if eligible/expected_facts<minimum['eligible_selected_m15_coverage_per_product_minimum']:
            raise ValueError('eligible closed M15 coverage below threshold')
        admission.append(dict(csymbol=cs,expected_minutes=denominator,received_minutes=numerator,
            expected_m15=expected_facts,replay_m15=actual_facts,eligible_m15=eligible))
    # Keep old selected legs for managing exits; no pre-score decisions are iterated.
    for symbol,leg in data['legs'].items():
        if leg['cs'] in universe:view['legs'][symbol]=leg
    return view,admission


def execute(protocol_path,inputs,output,study_dir=None,synthetic=False):
    protocol_path=Path(protocol_path);inputs=Path(inputs);output=Path(output).resolve()
    if any(part.lower() in ('reports','config','docs','tools','tests','backtest','futures_trend_quant','.git') for part in output.parts):
        raise ValueError('output inside protected project directory')
    if output==ROOT or output.exists() or output.is_relative_to(inputs.resolve()):raise ValueError('output must be a fresh isolated directory')
    reg=read(protocol_path);lock=read(inputs/'input_lock.json');protocol_sha=canonical_digest(protocol_path)
    if lock['protocol_sha256']!=protocol_sha:raise ValueError('independent protocol hash mismatch')
    if sorted(lock['score_trade_dates'])!=reg['score_trade_dates']:raise ValueError('input date set mismatch')
    if synthetic!=(lock['data_kind']=='SYNTHETIC'):raise ValueError('synthetic/real evidence cannot mix')
    if synthetic and not reg['study'].startswith('SYNTHETIC_'):raise ValueError('synthetic protocol required')
    if not synthetic:
        frozen=read(ROOT/'tools/short_swing_v9/independent_freeze.json')
        if frozen['protocol_sha256']!=protocol_sha:raise ValueError('not the frozen independent protocol')
        if reg['status']!='FROZEN_FOR_QUALIFIED_DATA':raise ValueError('real study not ready for scoring')
        if not lock.get('calendar_independent_source_verified') or not lock.get('mapping_asof_verified'):
            raise ValueError('calendar or mapping source not qualified')
        if not study_dir:raise ValueError('unique study registry required')
        if Path(study_dir).resolve()!=(ROOT/'data/cache/independent_v9_registry').resolve():raise ValueError('wrong study registry')
    if set(lock['engine_hashes'])!=REQUIRED_ENGINE_FILES:raise ValueError('incomplete engine binding')
    for path,sha in lock['source_hashes'].items():
        source=(inputs/path).resolve()
        if not source.is_relative_to(inputs.resolve()) or digest(source)!=sha:raise ValueError('source hash/path mismatch')
    for path,sha in lock['engine_hashes'].items():
        if canonical_digest(ROOT/path)!=sha:raise ValueError('engine hash mismatch')
    if lock['engine_hashes']!=reg['engine_hashes']:raise ValueError('protocol engine binding mismatch')
    if digest(inputs/'prepared.pkl')!=lock['prepared_sha256']:raise ValueError('prepared input hash mismatch')
    check_lock()
    with (inputs/'prepared.pkl').open('rb') as f:data=pickle.load(f)
    # The calendar is separately supplied and hashed, never inferred from received bars.
    calendar=read(inputs/'calendar.json')
    if digest(inputs/'calendar.json')!=lock['calendar_sha256']:raise ValueError('calendar hash mismatch')
    for cs,clock in data['clocks'].items():
        records=calendar['products'][cs]
        if [int(pd.Timestamp(r['eob']).value) for r in records]!=list(clock['ns']) or [r['trading_date'] for r in records]!=list(clock['days']):
            raise ValueError('prepared clock differs from independent calendar')
    view,admission=scoring_view(data,reg,synthetic)
    token=None
    if not synthetic:
        registry=Path(study_dir);registry.mkdir(parents=True,exist_ok=True);token=registry/'independent_consumed.json'
        with token.open('x',encoding='utf8') as stream:
            json.dump(dict(protocol_sha256=protocol_sha,input_lock_sha256=digest(inputs/'input_lock.json'),state='STARTED_TEST_CONSUMED'),stream)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        stage=Path(temporary)/'results';stage.mkdir();summaries={}
        for scenario in SCENARIOS:
            costs=None if scenario=='GROSS' else read(ROOT/'tools/full_trade_v8/cost_reference.json')
            slip=0 if costs is None else int(scenario[-2]);summaries[scenario]={}
            for group in ('S0','ORDINARY'):
                account=ShortSwingAccount(view,group,costs,slip,reg.get('holding_policy'))
                assert not account.positions and not account.pending and account.cash==1_000_000
                result=account.run();summaries[scenario][group]=summarize(result)
                for name in ('trades','opened','events','curve'):
                    frame=pd.DataFrame(result[name])
                    if frame.empty:frame=pd.DataFrame(columns=['group','symbol','entry_bob','exit_time','modeled_net_pnl','status'])
                    frame.to_csv(stage/(scenario+'_'+group+'_'+name+'.csv'),index=False)
        if any(canonical_digest(ROOT/path)!=sha for path,sha in lock['engine_hashes'].items()):raise ValueError('source changed during run')
        write(stage/'summary.json',dict(groups=summaries,protocol_sha256=protocol_sha,admission=admission,
            status='SYNTHETIC_SMOKE_ONLY' if synthetic else 'INDEPENDENT_ACCOUNT_REPLAY_COMPLETE',
            input_lock_sha256=digest(inputs/'input_lock.json'),prewarm_account_positions=0,prewarm_pending_intents=0,
            unique_real_test_consumed=not synthetic,real_orders=False,costs_certified=bool(lock.get('costs_certified',False))))
        stage.rename(output)
    return read(output/'summary.json')


def main():
    p=argparse.ArgumentParser();p.add_argument('--protocol',type=Path,required=True);p.add_argument('--inputs',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--study-dir',type=Path);p.add_argument('--synthetic',action='store_true');a=p.parse_args()
    result=execute(a.protocol,a.inputs,a.output,a.study_dir,a.synthetic);print(result['status'])


if __name__=='__main__':main()
