"""Production supervisor clock handoff, including genuine future/stale failures."""
from types import SimpleNamespace
from unittest.mock import Mock
import json
import pandas as pd
import pytest
from futures_trend_quant.live.paper_five import FiveRunner
from futures_trend_quant.live.paper_progress import progress_problem
from futures_trend_quant.live.paper_io import JsonReadError
from futures_trend_quant.live import paper_io
import futures_trend_quant.live.paper_five as five

NOW=pd.Timestamp('2026-11-10T16:22:10+08:00')


def fixture(tmp_path,monkeypatch,proof,checked):
    r=FiveRunner.__new__(FiveRunner);r.readonly=False;r.c={'strategy_name':'S0_SHORT_PAPER_CORE_V2'}
    r.evidence=tmp_path;r.run_id='CROSS_PROCESS_CLOCK';r.event=Mock();r.notice=Mock()
    r.engine=SimpleNamespace(s={'pause_new':True},pause_entries=Mock())
    r.trend_notifier=SimpleNamespace(pid=123,poll=lambda:None)
    r.trend_notifier_started=NOW-pd.Timedelta(minutes=2)
    monkeypatch.setattr(five,'read_json',lambda path:proof)
    monkeypatch.setattr(five,'pd',SimpleNamespace(Timestamp=SimpleNamespace(now=lambda **kw:checked)))
    return r


def health(at,**extra):
    return dict(pid=123,at=str(at),status='COMPLETED',stage='M5_DIVERGENCE',
                stage_started_at=str(at-pd.Timedelta(milliseconds=10)),stage_failures={},**extra)


def test_proof_published_after_cycle_start_is_judged_at_actual_read_completion(tmp_path,monkeypatch):
    proof=health(NOW+pd.Timedelta(milliseconds=20));checked=NOW+pd.Timedelta(milliseconds=40)
    # Exact original regression: using the old cycle clock falsely says FUTURE.
    assert progress_problem(proof,123,NOW)=='TREND_NOTIFIER_HEALTH_STALE'
    r=fixture(tmp_path,monkeypatch,proof,checked)
    r.supervise_trend_notifier(NOW,5)
    r.engine.pause_entries.assert_not_called();r.event.assert_not_called();r.notice.assert_not_called()
    assert r.engine.s['pause_new'] is True  # No restoration hidden inside health recovery.


@pytest.mark.parametrize('case',['stale','genuine_future','wrong_pid','hung_stage','failed_stage'])
def test_read_completion_clock_never_weakens_actual_failure_guards(tmp_path,monkeypatch,case):
    checked=NOW+pd.Timedelta(milliseconds=40);proof=health(NOW)
    if case=='stale':proof=health(NOW-pd.Timedelta(seconds=31))
    if case=='genuine_future':proof=health(checked+pd.Timedelta(milliseconds=1))
    if case=='wrong_pid':proof['pid']=999
    if case=='hung_stage':proof.update(status='RUNNING',stage_started_at=str(NOW-pd.Timedelta(seconds=31)))
    if case=='failed_stage':proof['stage_failures']={'M5_DIVERGENCE':{'category':'PermissionError'}}
    r=fixture(tmp_path,monkeypatch,proof,checked);r.supervise_trend_notifier(NOW,5)
    r.engine.pause_entries.assert_called_once();r.notice.assert_called_once()
    event=r.event.call_args
    assert event.args[0]=='NOTIFICATION_HEALTH_FAULT'
    assert event.kwargs['diagnostic']['checked_at']==str(checked)
    assert event.kwargs['diagnostic']['proof_at']==proof['at']
    files=list(tmp_path.glob('trend_failure_*.local.json'));assert len(files)==1
    saved=json.loads(files[0].read_text());assert saved['diagnostic']['report_cycle_at']==str(NOW)


def test_unknown_proof_failure_keeps_exact_file_and_stage_and_recovers_without_clearing_pause(tmp_path,monkeypatch):
    proof=health(NOW);checked=NOW+pd.Timedelta(milliseconds=40)
    r=fixture(tmp_path,monkeypatch,proof,checked)
    error=JsonReadError(tmp_path/'trend_reminder_health.local.json','OPEN',PermissionError(13,'PRIVATE_PATH'),3)
    monkeypatch.setattr(five,'read_json',Mock(side_effect=error))
    r.supervise_trend_notifier(NOW,5)
    diagnostic=r.event.call_args.kwargs['diagnostic']['read_failure']
    assert diagnostic['file']=='trend_reminder_health.local.json' and diagnostic['stage']=='OPEN'
    assert diagnostic['errno']==13 and diagnostic['attempts']==3 and 'PRIVATE_PATH' not in str(diagnostic)
    monkeypatch.setattr(five,'read_json',lambda path:proof)
    r.supervise_trend_notifier(NOW,5)
    assert r.event.call_args.args[0]=='NOTIFICATION_HEALTH_RECOVERED'
    assert r.event.call_args.kwargs['pause_retained'] is True and r.engine.s['pause_new']


def test_replace_namespace_gap_retries_same_fresh_file_and_persistent_missing_is_blocked(tmp_path,monkeypatch):
    path=tmp_path/'trend_reminder_health.local.json';path.write_text(json.dumps(health(NOW)))
    original=paper_io.shared_open;calls=[]
    def transient(p):
        calls.append(str(p))
        if len(calls)<3:raise FileNotFoundError(2,'REPLACE_GAP')
        return original(p)
    monkeypatch.setattr(paper_io,'shared_open',transient)
    assert paper_io.read_json(path)['at']==str(NOW)
    assert calls==[str(path)]*3
    monkeypatch.setattr(paper_io,'shared_open',Mock(side_effect=FileNotFoundError(2,'MISSING')))
    with pytest.raises(JsonReadError) as error:paper_io.read_json(path)
    assert error.value.details['attempts']==3 and error.value.details['category']=='FileNotFoundError'
