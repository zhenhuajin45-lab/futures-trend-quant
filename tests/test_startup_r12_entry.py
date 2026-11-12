"""Opt-in R12 path through real entry and persisted WAL, no broker SDK."""
from pathlib import Path
import json
import pytest
import pandas as pd
from futures_trend_quant.live import paper_clock_health as ch
from test_paper_runtime_health_v6 import setup_health,write
from test_paper_execution import engine,signal,NOW


@pytest.mark.parametrize('case,reason',[('valid',None),('expired','SIGNAL_EXPIRED_OR_FUTURE'),
    ('service','SERVICE_RECOVERY_PENDING'),('quote','QUOTE_STALE'),('pause','STOP_NEW_OPEN'),
    ('position','PRODUCT_POSITION_OR_OPEN_INTENT')])
def test_r12_actual_open_reaches_wal_only_after_all_original_guards(tmp_path,case,reason):
    e,t=engine(tmp_path);setup_health(e.c);e.c['clock_startup_policy']=ch.STARTUP_POLICY
    s=signal();ev=Path(e.c['evidence_dir'])
    if case=='expired':s['signal_at']=str(NOW-pd.Timedelta(seconds=31))
    elif case=='service':
        p=ev/'service_health.local.json';row=json.loads(p.read_text());row['at']=str(NOW-pd.Timedelta(seconds=4));write(p,row)
    elif case=='quote':e.quotes['DCE.m2701']['created_at']=str(NOW-pd.Timedelta(seconds=4))
    elif case=='pause':e.s['pause_new']=True
    elif case=='position':e.snapshot['positions']=[dict(symbol='DCE.m2701',side=1,volume=1)]
    ok,why=e.open(s,NOW)
    if case=='valid':
        assert ok and len(t.jobs)==1 and t.jobs[0]['op']=='submit'
        persisted=json.loads(e.path.read_text());job=t.jobs[0]
        assert job['id'] in persisted['intents'] and job['args']['volume']==1 and job['args']['price']==101
        assert job['args']['signal_at']==s['signal_at'] and 'E1' in persisted['used_signals']
    else:
        assert not ok and reason in why and not t.jobs and not e.s['intents']
