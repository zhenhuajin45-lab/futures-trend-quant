"""Offline causal replay of explicit frozen envelopes. No vendor or broker API.

Input JSONL rows contain symbol/csymbol/m15/m5/received/tick/asof/trading_day/
session_id/source_prefix_sha256. Bars use the original Bar mapping schema.
The input is processed in supplied knowledge order, never sorted by price time.
"""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from futures_trend_quant.observation.price_action.models import DEFAULTS, clock, digest
from futures_trend_quant.observation.price_action.pipeline import Pipeline
from futures_trend_quant.observation.price_action.state import State, atomic
from futures_trend_quant.data.gm_bars import bars_from_records


def replay(source,output):
    source=Path(source);output=Path(output)
    if output.resolve().is_relative_to(source.resolve()):raise ValueError('INPUT_OUTPUT_OVERLAP')
    state=State(output/'state',digest(DEFAULTS));pipe=Pipeline(state);last=None;count=0
    raw=source.read_bytes();prefix=digest(raw.hex())
    for line in raw.decode('utf-8-sig').splitlines():
        if not line:continue
        row=json.loads(line);asof=clock(row['asof'])
        if last is not None and asof<last:raise ValueError('KNOWLEDGE_ORDER_REVERSED')
        last=asof
        if row.get('availability_basis') not in ('ACTUAL_RECORDED_CLOCKS','CAUSAL_REPLAY_SIMULATED_COMPUTE_AT_RECEIPT'):
            raise ValueError('REPLAY_AVAILABILITY_BASIS_REQUIRED')
        row['m15']=bars_from_records(row['m15']);row['m5']=bars_from_records(row['m5'])
        basis=row.pop('availability_basis');result=pipe.snapshot(**row)
        if result:
            result.update(availability_basis=basis,replay_input_sha256=prefix,
                          performance_claim='NONE;NO_FILL_MODEL_OR_REAL_ORDERS')
            atomic(output/'snapshots'/(result['event_id']+'.json'),result);count+=1
    receipt=dict(input_sha256=prefix,snapshots=count,mode='SHADOW',orders_sent=0,
                 economic_edge_verified=False,statistical_admission='INSUFFICIENT_UNLESS_SEPARATELY_REVIEWED')
    atomic(output/'replay_receipt.json',receipt)
    return receipt


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();print(json.dumps(replay(a.input,a.output)))
