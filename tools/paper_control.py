"""Local operator control: stop new opens, optionally supervise a full reduction."""
from pathlib import Path
import argparse
import json
import sys
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from futures_trend_quant.live.paper_config import load_paper_config
from futures_trend_quant.live.paper_broker import atomic


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--stop-new',action='store_true');p.add_argument('--flatten',action='store_true')
    a=p.parse_args();c=load_paper_config(a.config)
    if not a.stop_new and not a.flatten:raise SystemExit('select --stop-new or --flatten')
    atomic(Path(c['state_dir'])/'control.json',dict(stop_new_open=True,flatten=a.flatten,at=datetime.now(timezone.utc).isoformat(),
        authority='LOCAL_OPERATOR_CONTROL; ACTUAL_REDUCTION_REQUIRES_CONFIRMED_POSITION_AND_QUOTE'))
    print('NEW_OPENS_STOPPED; EXISTING_RISK_REMAINS_UNTIL_BROKER_FLAT_CONFIRMED')
