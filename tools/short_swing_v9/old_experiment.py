"""One preregistered holding-discipline comparison on the immutable old inputs."""
from pathlib import Path
import argparse,pickle
import pandas as pd
from backtest.forward_v3 import read,write,digest,check_lock
from tools.historical_strategy.reproduce import canonical_digest
from tools.full_trade_v8.run import summarize
from .account import ShortSwingAccount

ROOT=Path(__file__).resolve().parents[2]


def main():
    p=argparse.ArgumentParser();p.add_argument('--inputs',type=Path,required=True);p.add_argument('--source-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--scenario',choices=['GROSS','REFERENCE_1T','REFERENCE_2T','REFERENCE_3T'],required=True)
    a=p.parse_args();reg=read(ROOT/'tools/short_swing_v9/old_experiment_registration.json')
    if a.output.exists() or 'reports' in a.output.resolve().parts:raise ValueError('fresh local audit output required')
    lock=read(a.inputs/'input_lock.json')
    if digest(a.inputs/'input_lock.json')!=reg['old_input_lock_sha256']:raise ValueError('old sample changed')
    for path,sha in reg['engine_hashes'].items():
        if canonical_digest(ROOT/path)!=sha:raise ValueError('registered code changed')
    for name,sha in lock['source_hashes'].items():
        if digest(a.source_root/name)!=sha:raise ValueError('raw/facts source changed')
    if digest(a.inputs/'prepared.pkl')!=lock['prepared_sha256']:raise ValueError('prepared input changed')
    check_lock()
    with (a.inputs/'prepared.pkl').open('rb') as stream:data=pickle.load(stream)
    costs=None if a.scenario=='GROSS' else read(ROOT/'tools/full_trade_v8/cost_reference.json');slip=0 if costs is None else int(a.scenario[-2])
    a.output.mkdir(parents=True);summary={}
    for arm,policy in [('FROZEN_S0',None),('SHORT_S0',reg['holding_policy'])]:
        result=ShortSwingAccount(data,'S0',costs,slip,policy).run();summary[arm]=summarize(result)
        for name in ('trades','opened','events','curve','exit_attempts'):
            frame=pd.DataFrame(result.get(name,[]))
            if frame.empty:frame=pd.DataFrame(columns=['group','trade_id','symbol','time','status'])
            frame.to_csv(a.output/(arm+'_'+name+'.csv'),index=False)
        print('V9_OLD_ACCOUNT_COMPLETED',a.scenario,arm,summary[arm]['closed_trades'],summary[arm]['total_marked_pnl'],flush=True)
    for path,sha in reg['engine_hashes'].items():
        if canonical_digest(ROOT/path)!=sha:raise ValueError('source changed during run')
    write(a.output/'summary.json',dict(study='ONE_OLD_WINDOW_HOLDING_DISCIPLINE_DIAGNOSTIC',scenario=a.scenario,groups=summary,
        registration_sha256=canonical_digest(ROOT/'tools/short_swing_v9/old_experiment_registration.json'),
        engine_hashes=reg['engine_hashes'],input_lock_sha256=reg['old_input_lock_sha256'],
        independent_validation=False,real_orders=False,execution_certified=False,
        fill_basis='SAME_V8_M1_PROXY; EXPLICIT_NO_QUOTE_LIMIT_OR_UNFILLED_KEEPS_POSITION',
        unknown_quote_evidence='NOT_PROOF_OF_REAL_FLAT',end_positions='MARKED_SEPARATELY'))


if __name__=='__main__':main()
