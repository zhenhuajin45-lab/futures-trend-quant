"""Final old-window entry comparison: reuse frozen short S0, run ordinary only."""
from pathlib import Path
import argparse,pickle
import pandas as pd
from backtest.forward_v3 import read,write,digest,check_lock
from tools.historical_strategy.reproduce import canonical_digest
from tools.full_trade_v8.run import summarize
from tools.short_swing_v9.account import ShortSwingAccount

ROOT=Path(__file__).resolve().parents[2]


def main():
    p=argparse.ArgumentParser();p.add_argument('--inputs',type=Path,required=True);p.add_argument('--source-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--scenario',choices=['GROSS','REFERENCE_1T','REFERENCE_2T'],required=True)
    a=p.parse_args();reg=read(ROOT/'tools/same_discipline_v10/registration.json')
    if a.output.exists() or 'reports' in a.output.resolve().parts:raise ValueError('fresh audit output required')
    if digest(a.inputs/'input_lock.json')!=reg['input_lock_sha256']:raise ValueError('sample changed')
    for path,sha in reg['account_source_hashes'].items():
        if canonical_digest(ROOT/path)!=sha:raise ValueError('frozen account or rule changed')
    lock=read(a.inputs/'input_lock.json')
    for path,sha in lock['source_hashes'].items():
        if digest(a.source_root/path)!=sha:raise ValueError('raw or facts changed')
    if digest(a.inputs/'prepared.pkl')!=lock['prepared_sha256']:raise ValueError('prepared input changed')
    check_lock()
    with (a.inputs/'prepared.pkl').open('rb') as f:data=pickle.load(f)
    costs=None if a.scenario=='GROSS' else read(ROOT/'tools/full_trade_v8/cost_reference.json')
    slip=0 if costs is None else int(a.scenario[-2])
    result=ShortSwingAccount(data,'ORDINARY',costs,slip,reg['holding_policy']).run()
    summary=summarize(result)
    a.output.mkdir(parents=True)
    for name in ('trades','opened','events','curve','exit_attempts'):
        frame=pd.DataFrame(result.get(name,[]))
        if frame.empty:frame=pd.DataFrame(columns=['group','trade_id','symbol','time','status'])
        frame.to_csv(a.output/('ORDINARY_SHORT_'+name+'.csv'),index=False)
    for path,sha in reg['account_source_hashes'].items():
        if canonical_digest(ROOT/path)!=sha:raise ValueError('source changed during run')
    write(a.output/'summary.json',dict(study='FINAL_SAME_DISCIPLINE_ENTRY_COMPARISON',scenario=a.scenario,
        groups={'ORDINARY_SHORT':summary},registration_sha256=canonical_digest(ROOT/'tools/same_discipline_v10/registration.json'),
        input_lock_sha256=reg['input_lock_sha256'],account_source_hashes=reg['account_source_hashes'],
        s0_reused=True,new_strategy_group_count=0,independent_test_consumed=False,real_orders=False))
    print('ORDINARY_SHORT_COMPLETE',a.scenario,summary['closed_trades'],summary['total_marked_pnl'],flush=True)


if __name__=='__main__':main()
