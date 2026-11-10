"""Local descriptive SHADOW report, never reports hypothetical touches as fills."""
from collections import Counter
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from futures_trend_quant.live.paper_io import read_json
from futures_trend_quant.observation.price_action.state import atomic


def report(folder,output):
    rows={}
    for path in Path(folder).rglob('*.json'):
        r=read_json(path)
        if r.get('event_id') and r.get('policy_version','').startswith('PRICE_ACTION_SHADOW'):rows[r['event_id']]=r
    candidates=[r for r in rows.values() if r.get('candidate_evidence_id')]
    days={r['trading_day'] for r in rows.values()};products={r['csymbol'] for r in rows.values()}
    result=dict(snapshots=len(rows),independent_days=len(days),products=sorted(products),original_candidates=len(candidates),
        structures=dict(Counter(r['structure']['structure_state'] for r in rows.values())),
        locations=dict(Counter(r['location']['location_class'] for r in rows.values())),
        room=dict(Counter(r['room']['status'] for r in candidates)),
        coverage_floor_met=len(days)>=20 and len(candidates)>=200 and len(products)>=3,
        automatic_promotion_allowed=False,economic_edge_verified=False,real_orders_created_by_shadow=0,
        performance='NOT_EVALUATED;NO_EXECUTABLE_FILL_OR_COST_MODEL',
        required_followup=['TIME_FORWARD_TRAIN_VALIDATE_HOLDOUT','COST_SENSITIVITY','OVERLAPPING_EXPOSURE_PURGE',
                           'EXPECTANCY_UNCERTAINTY_DRAWDOWN_MAE_MFE','CORRELATED_SLOT_ATTRIBUTION'])
    atomic(output,result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();print(json.dumps(report(a.input,a.output),ensure_ascii=False))
