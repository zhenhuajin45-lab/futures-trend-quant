"""Actual repaired coordinator ordering, no SDK/native account activity."""
from tools.broker_busy_audit import offline_probe


def test_actual_cycle_applies_reply_then_defers_and_submits_candidate_once(tmp_path):
    r=offline_probe(tmp_path)
    assert not r['baseline_defect_reproduced']
    lost=r['local_query_busy_signal_lost']
    assert lost['flow'][0]=='DRAIN_SNAPSHOT' and lost['flow'][1]=='EVALUATE_SIGNAL'
    assert lost['signal_evaluations']==1 and lost['account_ready_after_snapshot']
    assert lost['intents_after_idle_cycle']==1 and lost['transport_jobs_after_idle_cycle']==1


def test_busy_is_local_and_released_eligible_signal_can_pass_without_changing_guards(tmp_path):
    r=offline_probe(tmp_path)
    assert not r['eligible_signal_would_pass_when_reevaluated'] and r['synthetic_queued_submit_count']==1
    assert r['native_api_calls']==0


def test_releasing_busy_never_implies_risk_limit_is_bypassed(tmp_path):
    r=offline_probe(tmp_path)
    assert r['unchanged_one_lot_risk_guard']=='ONE_LOT_RISK_LIMIT'
    assert r['over_budget_native_submit_count']==0
