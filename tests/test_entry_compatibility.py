"""Offline policy and real engine safety regressions; no GM connection."""
from dataclasses import asdict, replace
from datetime import timedelta
import pytest
from tools.entry_compatibility.policy import evaluate_selected
from tools.entry_compatibility.conditions import path_conditions, relation
from futures_trend_quant.core.pipeline.permissions import evaluate_permission
from futures_trend_quant.core.entry.models import EntryContext, EntrySettings, EvidenceSnapshot, EvidenceEvent
from futures_trend_quant.core.entry.paths import evaluate_path_a,evaluate_path_b,evaluate_path_c,evaluate_path_d_candidate


def facts(direction='bull'):
    return dict(close_side='above' if direction=='bull' else 'below',slope='up' if direction=='bull' else 'down',
                hist=-1 if direction=='bull' else 1,opposite_divergence=False,closed_h1_known=True,
                calendar_qualified=True,previous_weak=0,changed=False,eob='2026-11-06T22:00:00+08:00')


@pytest.mark.parametrize('direction',['bull','bear'])
def test_b_sign_advisory_mirrored_and_default_off(direction):
    kw=facts(direction);original=evaluate_permission(direction,**kw)
    assert evaluate_selected('B',direction,**kw)==original
    trial=evaluate_selected('B',direction,enabled=True,**kw)
    assert trial.new_entry_allowed and not original.new_entry_allowed
    for key in ('technical_exit_direction','reverse_authorized','weak_closed_bars','source_eob'):
        assert getattr(trial,key)==getattr(original,key)


@pytest.mark.parametrize('path',list('ACD'))
@pytest.mark.parametrize('direction',['bull','bear','none'])
def test_other_paths_byte_equivalent(path,direction):
    kw=facts(direction)
    assert asdict(evaluate_selected(path,direction,enabled=True,**kw))==asdict(evaluate_permission(direction,**kw))


@pytest.mark.parametrize('change',[dict(close_side='equal'),dict(slope='flat'),dict(opposite_divergence=True),dict(closed_h1_known=False),dict(calendar_qualified=False),dict(hist=None),dict(hist=float('nan')),dict(hist=float('inf'))])
def test_b_remaining_guards_reject(change):
    assert not evaluate_selected('B','bull',enabled=True,**dict(facts(),**change)).new_entry_allowed


def test_none_cannot_be_authorized_and_exit_not_weakened():
    assert not evaluate_selected('B','none',enabled=True,**facts()).new_entry_allowed
    kw=dict(facts(),close_side='below',previous_weak=1)
    r=evaluate_selected('B','bull',enabled=True,**kw)
    assert r.technical_exit_direction=='bull' and not r.new_entry_allowed


def test_matrix_matches_original_paths_and_exposes_unordered_b():
    ctx=EntryContext('bull',True,'bull_strong','up',False,True)
    settings=EntrySettings()
    pattern=EvidenceEvent('pattern','bull','early',1)
    pull=EvidenceEvent('pullback','bull','late',3)
    evidence=EvidenceSnapshot('bull',m5_pattern=pattern,m5_pullback=pull)
    functions=[evaluate_path_a,evaluate_path_b,evaluate_path_c,evaluate_path_d_candidate]
    for path,fn in zip('ABCD',functions):
        assert all(path_conditions(ctx,evidence,settings)[path].values())==(fn(ctx,evidence,settings) is not None)
    assert evaluate_path_b(ctx,evidence,settings) is not None
    assert relation(pull,pattern)['ordering']=='CONFIRM_BEFORE_FIRST'
    assert relation(pull,pattern)['mechanism_qualified'] is None
    assert relation(pull,pattern)['mechanism_status']=='UNKNOWN'


def test_72s_inflight_snapshot_cannot_be_preempted_but_exit_survives(tmp_path):
    from test_paper_execution import engine,signal,ack,position,snapshot,quote,NOW
    e,t=engine(tmp_path);assert e.open(signal(),NOW)[0]
    opening=ack(e,status=3,filled=1)
    e.apply_snapshot(snapshot(orders=[opening],positions=[position()]),NOW)
    t.jobs.clear();t.is_busy=True  # a snapshot already occupies the only channel
    e.force_exit('DCE.m2701','STOP_M1_TRIGGER',NOW)
    for seconds in (1,30,71):
        now=NOW+timedelta(seconds=seconds);e.quote('DCE.m2701',quote(now),now);e.tick(now)
        assert not t.jobs and e.positions()[0]['volume']==1
        due=next(iter(e.s['due_events'].values()))
        assert not due['completed'] and due['wait_reason']=='BROKER_BUSY'
    now=NOW+timedelta(seconds=72);t.is_busy=False
    e.tick(now)
    assert not t.jobs  # stale broker truth still prevents sending
    e.apply_snapshot(snapshot(orders=[opening],positions=[position()]),now)
    e.quote('DCE.m2701',quote(now),now);e.tick(now)
    assert len(t.jobs)==1 and t.jobs[0]['args']['position_effect']!=1
    assert e.positions()[0]['volume']==1 and not next(iter(e.s['due_events'].values()))['completed']
    close=t.jobs[0]
    closing=dict(opening,cl_ord_id='CLOSE_STALL_FIXTURE',side=2,position_effect=2,status=3,filled_volume=1,filled_vwap=100.)
    e.accept(close,dict(ok=True,data=[closing]),now)
    e.apply_snapshot(snapshot(orders=[opening,closing]),now);e.tick(now)
    assert next(iter(e.s['due_events'].values()))['completed']
