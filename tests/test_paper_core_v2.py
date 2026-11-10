"""Production-shaped deterministic regressions. No SDK login or test orders."""
from dataclasses import replace,asdict
from datetime import datetime,timedelta,timezone
import json
import pytest
from helpers import make_bars
from test_paper_execution import engine,signal,NOW,quote,snapshot,PaperEngine
from futures_trend_quant.core.facts.pipeline import aggregate_m15_to_h1_with_coverage,aggregate_m15_to_h1
from futures_trend_quant.core.facts_state import FactsState,StableDivergence,latest_valid_divergence
from futures_trend_quant.core.pipeline.permissions import evaluate_permission
from futures_trend_quant.live.paper_anchor_guard import validate_execution_proof


def night(n=24):
    begin=datetime(2026,11,5,21,tzinfo=timezone(timedelta(hours=8)))
    return [replace(b,symbol='SHFE.au2612',bob=begin+timedelta(minutes=15*i),eob=begin+timedelta(minutes=15*(i+1)))
            for i,b in enumerate(make_bars(n))]


def test_all_prefixes_keep_published_h1_immutable():
    bars=night(22);published={}
    for n in range(1,23):
        aggregates=aggregate_m15_to_h1_with_coverage(bars[:n])
        assert sum(len(x.source_bar_ids) for x in aggregates)==n
        assert all(len(x.source_bar_ids)<=4 for x in aggregates)
        for x in aggregates:
            if x.closed and x.complete:
                key=x.bucket_start
                if key in published:assert published[key]==x
                else:published[key]=x
    assert [len(x.source_bar_ids) for x in published.values()]==[4,4,4,4,4,2]
    assert len(aggregate_m15_to_h1(bars[:5]))==1
    assert len(aggregate_m15_to_h1(bars[:7]))==1
    assert len(aggregate_m15_to_h1(bars[:8]))==2


def test_missing_source_is_not_repaired_by_later_bar_or_extreme_price():
    bars=night(8)
    result=aggregate_m15_to_h1_with_coverage([*bars[:2],*bars[3:]])
    assert result[0].closed and not result[0].complete
    assert len(aggregate_m15_to_h1([*bars[:2],*bars[3:]]))==1


@pytest.mark.parametrize('kind,broken_field,extreme,breach',[('top','high',105.,106.),('bottom','low',95.,94.)])
def test_divergence_tombstone_survives_rolling_and_restart(kind,broken_field,extreme,breach):
    bars=night(8);event=StableDivergence(kind,'B_hist',bars[0].identity,bars[1].identity,bars[2].identity,3,('hist',),extreme)
    base=[replace(b,open=100,high=104,low=96,close=100) for b in bars]
    state=FactsState(divergences=[event])
    assert latest_valid_divergence(state,base[:4],kind)==event
    base[4]=replace(base[4],**{broken_field:breach})
    assert latest_valid_divergence(state,base[:5],kind) is None
    restored=FactsState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert latest_valid_divergence(restored,base[5:],kind) is None
    assert len(restored.invalidated)==1


@pytest.mark.parametrize('mutation',[
    {'anchor':110}, {'anchor_bar_id':'SHFE.au2612|900s|2026-11-05T09:30:00+08:00'},
    {'anchor_episode_id':'OTHER'}, {'anchor_confirmed_at':'2026-11-05T10:15:00+08:00'}, {'anchor_validated':False}])
def test_final_engine_exit_blocks_invalid_proof_without_order_intent(tmp_path,mutation):
    e,t=engine(tmp_path);s=signal();s.update(mutation)
    assert e.open(s,NOW)==(False,'INVALID_ANCHOR')
    assert not t.jobs and not e.s['intents']


def test_busy_candidate_waits_then_submits_once_with_original_clock(tmp_path):
    e,t=engine(tmp_path);t.is_busy=True
    assert e.offer(signal(),NOW)[1]=='CANDIDATE_WAITING'
    e.tick(NOW);e.drain_candidates(NOW)
    assert not t.jobs and len(e.pending)==1
    later=NOW+timedelta(seconds=1);t.is_busy=False
    e.apply_snapshot(snapshot(),later);e.quote('DCE.m2701',quote(later),later)
    e.tick(later);e.drain_candidates(later);e.drain_candidates(later)
    assert len(t.jobs)==1 and len(e.s['intents'])==1 and not e.pending
    assert next(iter(e.s['intents'].values()))['signal_at']==NOW.isoformat()
    assert e.offer(signal(now=NOW+timedelta(minutes=15)),NOW+timedelta(minutes=15))[1]=='CANDIDATE_SUBMITTED'


def test_candidate_expiry_pause_and_restart_never_resend(tmp_path):
    e,t=engine(tmp_path);e.offer(signal(),NOW);t.is_busy=True
    e.drain_candidates(NOW+timedelta(seconds=31));assert not e.pending and not t.jobs
    e.offer(signal(eid='E2'),NOW);e.s['pause_new']=True;e.drain_candidates(NOW)
    assert e.s['candidate_status']['E2']['status']=='BLOCKED'
    e.s['pause_new']=False;e.offer(signal(eid='E3'),NOW)
    restarted=PaperEngine(e.c,e.calendar,t)
    assert not restarted.pending and restarted.s['candidate_status']['E3']['status']=='ABORTED_RESTART'


def test_startup_backlog_is_rejected_before_queue_and_never_renewed(tmp_path):
    e,t=engine(tmp_path);later=NOW+timedelta(minutes=30)
    assert e.offer(signal(),later)[1]=='CANDIDATE_EXPIRED_BEFORE_QUEUE'
    assert not e.pending and not t.jobs and not e.s['intents']
    assert e.offer(signal(now=later),later)[1]=='CANDIDATE_EXPIRED_BEFORE_QUEUE'


def test_break_while_waiting_cannot_recover_when_price_rebounds(tmp_path):
    e,t=engine(tmp_path);e.offer(signal(),NOW);t.is_busy=True
    later=NOW+timedelta(seconds=1)
    e.quote('DCE.m2701',dict(quote(later),last=98),later)
    t.is_busy=False;e.quote('DCE.m2701',quote(later),later);e.drain_candidates(later)
    assert not t.jobs and not e.pending
    assert e.offer(signal(),later)[1]=='CANDIDATE_INVALIDATED'


def test_wait_does_not_chase_a_worse_price(tmp_path):
    e,t=engine(tmp_path);e.c['strategy_name']='S0_SHORT_PAPER_CORE_V2'
    s=signal();s.update(evidence_created_at=(NOW-timedelta(minutes=5)).isoformat(),first_eligible_at=NOW.isoformat(),
        expires_at=(NOW+timedelta(seconds=30)).isoformat(),decision_at=NOW.isoformat(),entry_permission=True,
        permission_policy='CLOSED_FACTS_PERMISSION_V1',permission_eob=NOW.isoformat(),permission_direction='bull')
    e.offer(s,NOW);later=NOW+timedelta(seconds=1)
    e.apply_snapshot(snapshot(),later);e.quote('DCE.m2701',dict(quote(later),ask=102),later)
    e.drain_candidates(later)
    assert not t.jobs and not e.pending
    assert 'PRICE_MOVED' in e.s['candidate_status']['E1']['reason']


@pytest.mark.parametrize('direction', ['bull','bear'])
def test_background_entry_exit_and_reverse_are_separate(direction):
    opposite='below' if direction=='bull' else 'above'
    kwargs=dict(close_side=opposite,slope='flat',hist=-1 if direction=='bull' else 1,
                opposite_divergence=True,closed_h1_known=True,calendar_qualified=True,changed=False,eob=NOW.isoformat())
    one=evaluate_permission(direction,previous_weak=0,**kwargs)
    two=evaluate_permission(direction,previous_weak=one.weak_closed_bars,**kwargs)
    assert two.background==direction and not two.new_entry_allowed
    assert one.technical_exit_direction is None and two.technical_exit_direction==direction
    assert two.reverse_authorized is None
    kwargs.update(close_side='above' if direction=='bull' else 'below',slope='up' if direction=='bull' else 'down',
                  hist=1 if direction=='bull' else -1,opposite_divergence=False)
    recovered=evaluate_permission(direction,previous_weak=2,**kwargs)
    assert recovered.new_entry_allowed and recovered.weak_closed_bars==0


def test_final_native_clock_contract_rejects_renewal_and_expiry():
    s=dict(signal_at=NOW.isoformat(),evidence_created_at=(NOW-timedelta(minutes=5)).isoformat(),
           first_eligible_at=NOW.isoformat(),expires_at=(NOW+timedelta(seconds=30)).isoformat(),
           decision_at=(NOW+timedelta(seconds=1)).isoformat(),entry_permission=True,
           permission_policy='CLOSED_FACTS_PERMISSION_V1',permission_eob=NOW.isoformat())
    validate_execution_proof(s,(NOW+timedelta(seconds=2)).to_pydatetime())
    with pytest.raises(ValueError):validate_execution_proof(s,(NOW+timedelta(seconds=31)).to_pydatetime())
    with pytest.raises(ValueError):validate_execution_proof(dict(s,expires_at=(NOW+timedelta(minutes=15)).isoformat()),(NOW+timedelta(seconds=2)).to_pydatetime())


@pytest.mark.parametrize('permission_policy',['CLOSED_FACTS_PERMISSION_V1','CLOSED_FACTS_PERMISSION_V2_20261111'])
def test_full_engine_handoff_reaches_native_guard_with_all_proofs(tmp_path,monkeypatch,permission_policy):
    import pandas as pd
    from test_paper_bridge import invoke,req
    e,t=engine(tmp_path/'engine');now=pd.Timestamp.now(tz='Asia/Shanghai')
    e.c['strategy_name']='S0_SHORT_PAPER_CORE_V2'
    e.apply_snapshot(snapshot(),now);e.quote('DCE.m2701',quote(now),now)
    s=signal(now);s.update(evidence_created_at=(now-timedelta(minutes=5)).isoformat(),
        first_eligible_at=now.isoformat(),expires_at=(now+timedelta(seconds=30)).isoformat(),decision_at=now.isoformat(),
        entry_permission=True,permission_policy=permission_policy,permission_eob=now.isoformat(),permission_direction='bull',reference_opponent_price=101.)
    # Entry calendar fixed for this no-network fixture, independent of test time.
    monkeypatch.setattr(e.calendar,'entry_block',lambda _:None)
    assert e.open(s,now)[0]
    payload=t.jobs[0]['args']
    (tmp_path/'bridge').mkdir()
    result,calls=invoke(tmp_path/'bridge',monkeypatch,[req('submit','V2',arguments=payload)],
        profile_extra={'strategy_name':'S0_SHORT_PAPER_CORE_V2'})
    assert result[0]['ok'] and len([c for c in calls if c[0]=='submit'])==1


@pytest.mark.parametrize('bad_field,bad_value', [('anchor_episode_id','ANOTHER_EPISODE'),('anchor_validated',False),
    ('entry_permission',False),('permission_policy','UNKNOWN')])
def test_native_exit_rejects_proof_without_order_call(tmp_path,monkeypatch,bad_field,bad_value):
    from test_paper_bridge import invoke,req,args
    from datetime import datetime,timezone
    now=datetime.now(timezone.utc)
    payload=args(now);payload.update(evidence_created_at=(now-timedelta(minutes=5)).isoformat(),
        first_eligible_at=now.isoformat(),expires_at=(now+timedelta(seconds=30)).isoformat(),decision_at=now.isoformat(),
        entry_permission=True,permission_policy='CLOSED_FACTS_PERMISSION_V1',permission_eob=now.isoformat(),permission_direction='bull',reference_opponent_price=101.)
    payload[bad_field]=bad_value
    result,calls=invoke(tmp_path,monkeypatch,[req('submit','INVALID',arguments=payload)],
        profile_extra={'strategy_name':'S0_SHORT_PAPER_CORE_V2'})
    assert not result[0]['ok'] and not any(c[0]=='submit' for c in calls)


def test_waiting_entry_cannot_jump_a_risk_exit(tmp_path):
    from test_paper_execution import ack,position
    e,t=engine(tmp_path);assert e.open(signal(),NOW)[0]
    row=ack(e,status=3,filled=1);e.apply_snapshot(snapshot(orders=[row],positions=[position()]),NOW)
    e.c['universe']=['DCE.M','DCE.EG']
    pending=signal(eid='EG');pending.update(symbol='DCE.eg2701',anchor_bar_id=signal()['anchor_bar_id'].replace('DCE.m2701','DCE.eg2701'))
    e.quote('DCE.eg2701',quote(),NOW);e.offer(pending,NOW)
    e.force_exit('DCE.m2701','TECHNICAL_WEAKNESS_EXIT_V1',NOW)
    before=len(t.jobs);e.tick(NOW);e.drain_candidates(NOW)
    assert len(t.jobs)==before+1 and t.jobs[-1]['args']['position_effect']!=1
    assert 'EG' in e.pending and not any(i['symbol']=='DCE.eg2701' for i in e.s['intents'].values())


@pytest.mark.parametrize('path',['A','B','C','D'])
def test_all_core_paths_share_permission_gate(monkeypatch,path):
    from test_v3_entry_h1 import _commodity_sessions
    from futures_trend_quant.core.pipeline import step as core
    from futures_trend_quant.core.entry.models import EntryDecision
    from futures_trend_quant.core.models import TrendState
    from futures_trend_quant.config import load_config,default_config_path
    bars=tuple(b for day in _commodity_sessions(.2) for b in day)
    rules=dict(load_config(default_config_path()).raw['rules'],paper_permission_v1=True,anchor_mode='range20')
    original=core.trend_facts
    monkeypatch.setattr(core,'trend_facts',lambda a,s:replace(original(a,s),close_side='below',r_bearish_reverse=False,
        s2_bearish_pattern=False,top_divergence=False))
    monkeypatch.setattr(core,'evaluate_entries',lambda *a:EntryDecision(True,'bull','TEST',path,bars[-1].identity,'TEST|'+path))
    state=core.StrategyRuntimeBook();state.trends['SHFE.AU']=TrendState(trend_state='bull',since_bar_id=bars[0].identity)
    r=core.run_core_step(core.CoreStepInput('SHFE.AU',bars[-1].symbol,bars,(),(),rules),state)
    assert r.trend.confirmed_state=='bull'
    assert not r.entry.accepted and r.entry.reason.startswith('SHARED_PERMISSION_BLOCK:')
    assert r.permission.new_entry_allowed is False


def test_owned_notifier_starts_once_and_failure_pauses_without_restart(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from futures_trend_quant.live.paper_five import FiveRunner
    import subprocess
    root=tmp_path/'_strategy_runtime';ev=root/'evidence/paper_observation_five';ev.mkdir(parents=True)
    r=FiveRunner.__new__(FiveRunner);r.readonly=False;r.c=dict(strategy_name='S0_SHORT_PAPER_CORE_V2',root=str(root))
    r.evidence=ev;r.engine,_=engine(tmp_path/'real_engine');r.event=Mock();r.notice=Mock();r.run_id='SYNTHETIC'
    process=SimpleNamespace(pid=99,poll=lambda:None)
    popen=Mock(return_value=process);monkeypatch.setattr(subprocess,'Popen',popen)
    r.supervise_trend_notifier(NOW,4);popen.assert_not_called()
    r.supervise_trend_notifier(NOW,5);popen.assert_called_once()
    r.supervise_trend_notifier(NOW+timedelta(seconds=61),5)
    assert r.engine.s['pause_new'] and r.trend_notifier_problem=='TREND_NOTIFIER_HEALTH_UNKNOWN'
    r.supervise_trend_notifier(NOW+timedelta(seconds=65),5);popen.assert_called_once()


@pytest.mark.parametrize('direction',['bull','bear'])
def test_real_coordinator_technical_exit_reduces_the_weakened_side(tmp_path,monkeypatch,direction):
    from types import SimpleNamespace
    from collections import Counter
    from unittest.mock import Mock
    import queue
    from test_paper_execution import ack,position
    from futures_trend_quant.live import paper_five as five
    from futures_trend_quant.core.models import Bar
    e,t=engine(tmp_path);s=signal()
    if direction=='bear':s.update(direction='bear',anchor=103.)
    assert e.open(s,NOW)[0]
    row=ack(e,status=3,filled=1);pos=position();pos['side']=1 if direction=='bull' else 2
    e.apply_snapshot(snapshot(orders=[row],positions=[pos]),NOW)
    bar=Bar('DCE.m2701','900s',NOW-timedelta(minutes=15),NOW,100,102,98,100)
    permission=SimpleNamespace(new_entry_allowed=False,technical_exit_direction=direction)
    result=SimpleNamespace(permission=permission,entry=None,gate=None,prior_trend=direction,
        trend=SimpleNamespace(confirmed=False,confirmed_state=direction),trend_changed=False,
        analysis=SimpleNamespace(bar=bar,finalized_h1_absolute=direction+'_strong',finalized_h1_eob=NOW),
        facts_row=dict(slope_state='flat'))
    out=queue.Queue();out.put(('CORE_RESULT',result,bar,NOW.isoformat()))
    runner=five.FiveRunner.__new__(five.FiveRunner);runner.engine=e;runner.evidence=e.evidence
    runner.products={'DCE.M':dict(core=SimpleNamespace(outbox=out),counts=Counter())};runner.counts=Counter();runner.notice=Mock()
    monkeypatch.setattr(five,'pd',SimpleNamespace(Timestamp=SimpleNamespace(now=lambda **k:NOW)))
    runner.core_events(NOW)
    assert e.s['meta']['DCE.m2701|'+direction]['force_exit']=='TECHNICAL_WEAKNESS_EXIT_V1'
    before=len(t.jobs);e.tick(NOW)
    assert len(t.jobs)==before+1 and t.jobs[-1]['args']['position_effect']==2
    assert t.jobs[-1]['args']['side']==(2 if direction=='bull' else 1)
