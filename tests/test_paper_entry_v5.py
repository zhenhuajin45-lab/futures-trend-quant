"""Path selection, persistent clocks and actual final bridge proof chain."""
from datetime import datetime,timedelta,timezone
from dataclasses import replace,asdict
import pytest
from futures_trend_quant.core.entry.models import EntryContext,EntrySettings,EvidenceEvent,EvidenceSnapshot,PathCandidate
from futures_trend_quant.core.entry.v5 import select
from futures_trend_quant.live.paper_anchor_guard import validate_execution_proof
from test_paper_execution import NOW


def fixture(hist=-1,direction='bull',now=NOW):
    symbol='DCE.m2701';start=symbol+'|300s|'+(now-timedelta(minutes=15)).isoformat();confirm=symbol+'|300s|'+(now-timedelta(minutes=5)).isoformat()
    pid=symbol+'|'+direction+'|'+start
    b=PathCandidate('B',direction,confirm,10,now-timedelta(minutes=5),pid)
    evidence=EvidenceSnapshot(direction,m5_consolidation=EvidenceEvent('box',direction,'box',10,now-timedelta(minutes=5)),m5_breakout=EvidenceEvent('break',direction,'newer',11,now))
    ctx=EntryContext(direction,True,direction+'_strong','up' if direction=='bull' else 'down',False,False,current_price=101,atr14=2)
    settings=EntrySettings(path_a_enabled=False,path_d_enabled=False,cooldown_enabled=False)
    facts=dict(close_side='above' if direction=='bull' else 'below',slope=ctx.m15_slope_state,hist=hist,opposite_divergence=False,closed_h1_known=True,calendar_qualified=True,previous_weak=0,changed=False,eob=now.isoformat())
    return ctx,evidence,settings,b,facts


def choose(f,clocks=None,at=NOW):
    ctx,e,s,b,p=f
    return select(ctx,e,s,b,facts=p,clocks={} if clocks is None else clocks,csymbol='DCE.M',eob=at)


@pytest.mark.parametrize('direction,hist',[('bull',-1),('bear',1)])
def test_unqualified_newer_c_cannot_hide_qualified_b(direction,hist):
    e,p,clock,audit=choose(fixture(hist,direction))
    assert e.accepted and e.path=='B' and p.new_entry_allowed
    assert audit[1]['path']=='C' and not audit[1]['eligible']


def test_both_qualified_keep_original_latest_and_tie_priority():
    f=fixture(hist=1);assert choose(f)[0].path=='C'
    ctx,e,s,b,p=f;e=replace(e,m5_breakout=replace(e.m5_breakout,sequence=10))
    assert choose((ctx,e,s,b,p))[0].path=='B'


def test_competition_and_restart_never_renew_any_evidence_clock():
    import json
    from futures_trend_quant.core.pipeline.step import StrategyRuntimeBook
    book=StrategyRuntimeBook();f=fixture(hist=1);choose(f,book.evidence_clocks)
    restored=StrategyRuntimeBook.from_dict(json.loads(json.dumps(book.to_dict())))
    outcome=choose(f,restored.evidence_clocks,NOW+timedelta(minutes=15))
    assert not outcome[0].accepted and all('EVIDENCE_EXPIRED' in r['reasons'] for r in outcome[3] if r.get('evidence_id'))
    assert restored.evidence_clocks==book.evidence_clocks


@pytest.mark.parametrize('problem',['consumed','no_h1','opposite','m5_reverse','h1_reverse','invalidated'])
def test_b_advisory_keeps_other_vetoes(problem):
    ctx,e,s,b,p=fixture()
    if problem=='consumed':ctx=replace(ctx,used_evidence_ids=frozenset([b.evidence_id]))
    if problem=='no_h1':p=dict(p,closed_h1_known=False)
    if problem=='opposite':p=dict(p,opposite_divergence=True)
    if problem=='m5_reverse':e=replace(e,m5_reverse_divergence=True)
    if problem=='h1_reverse':ctx=replace(ctx,h1_state='bear_strong')
    if problem=='invalidated':b=None
    assert not choose((ctx,e,s,b,p))[0].accepted


def v5_signal(now):
    from test_paper_execution import signal
    ctx,e,settings,b,facts=fixture(now=now)
    s=signal(now);s.update(path='B',core_evidence_id=b.evidence_id,
        evidence_created_at=b.trigger_eob.isoformat(),first_eligible_at=now.isoformat(),expires_at=(now+timedelta(seconds=30)).isoformat(),
        decision_at=now.isoformat(),entry_permission=True,permission_policy='PAPER_ENTRY_V5',permission_eob=now.isoformat(),permission_direction='bull',reference_opponent_price=101.)
    s['entry_policy_proof']=dict(policy='PAPER_ENTRY_V5',path='B',evidence_id=b.evidence_id,allowed=True,reasons=[],h1_absolute='bull_strong',b_h1_exception=False,m5_reverse_divergence=False,h1_source=dict(closed=True,calendar_qualified=True,entry_eob=(now-timedelta(hours=1)).isoformat()),**facts,
        b_process=dict(process_id=b.process_id,start_bar_id=b.process_id.split('|bull|')[-1],start_eob=(now-timedelta(minutes=15)).isoformat(),confirmation_bar_id=b.trigger_bar_id,confirmation_eob=b.trigger_eob.isoformat(),last_m5_eob=now.isoformat(),status='CONFIRMED_ACTIVE',session_id='SYNTHETIC_REGISTERED_SESSION',age=3))
    return s


def test_v5_actual_engine_handoff_and_native_guard_preserve_same_proof(tmp_path,monkeypatch):
    from test_paper_execution import engine,quote,snapshot
    from test_paper_bridge import invoke,req
    import pandas as pd
    e,t=engine(tmp_path/'engine');now=pd.Timestamp.now(tz='Asia/Shanghai')
    e.c.update(strategy_name='S0_SHORT_PAPER_CORE_V2',entry_policy_version='PAPER_ENTRY_V5',execution_policy_version='PAPER_EXEC_V5')
    e.apply_snapshot(snapshot(),now);e.quote('DCE.m2701',quote(now),now)
    monkeypatch.setattr(e.calendar,'entry_block',lambda _:None)
    s=v5_signal(now);outcome=e.offer(s,now);assert outcome==(False,'CANDIDATE_WAITING')
    e.drain_candidates(now);assert t.jobs,e.s['candidate_status']
    payload=t.jobs[0]['args'];assert payload['entry_policy_proof']==s['entry_policy_proof']
    (tmp_path/'bridge').mkdir()
    def before(root,profile):
        import json
        from futures_trend_quant.live.paper_config import digest
        from test_paper_execution import ACCOUNT
        (root/'state').mkdir();(root/'state/execution.json').write_text(json.dumps(dict(config_sha256=digest(profile),account_id=ACCOUNT,pause_new=False,halt_reason='',loss_cooldown={})))
        (root/'ev/latest_quotes').mkdir(parents=True)
        (root/'ev/latest_quotes/DCE.m2701.json').write_text(json.dumps(dict(config_sha256=digest(profile),symbol='DCE.m2701',received_at=now.isoformat(),quote=dict(quote(now),source='GM_BULK_CURRENT_POLL'))))
    replies,calls=invoke(tmp_path/'bridge',monkeypatch,[req('submit','V5',arguments=payload)],profile_extra=dict(strategy_name='S0_SHORT_PAPER_CORE_V2',entry_policy_version='PAPER_ENTRY_V5',execution_policy_version='PAPER_EXEC_V5',state_dir='state',evidence_dir='ev'),before_serve=before)
    assert replies[0]['ok'] and len([x for x in calls if x[0]=='submit'])==1


def test_friday_still_blocks_and_unknown_submit_cannot_switch_path(tmp_path):
    from test_paper_execution import engine,quote,snapshot
    e,t=engine(tmp_path);now=NOW.replace(day=6,hour=21,minute=15)
    e.apply_snapshot(snapshot(),now);e.quote('DCE.m2701',quote(now),now)
    assert not e.open(v5_signal(now),now)[0] and not t.jobs
    e2,t2=engine(tmp_path/'unknown');assert e2.open(v5_signal(NOW),NOW)[0]
    e2.accept(dict(op='submit',id=t2.jobs[0]['id']),dict(ok=False,native_submit_invoked=True,submit_may_have_reached_broker=True),NOW)
    n=len(t2.jobs);s=v5_signal(NOW);s.update(evidence_id='OTHER_PATH',path='C')
    assert not e2.open(s,NOW)[0] and len(t2.jobs)==n


def test_formal_ordered_book_checkpoint_sessions_and_consumption(monkeypatch):
    from types import SimpleNamespace
    from futures_trend_quant.core.entry import ordered_b as module
    from futures_trend_quant.core.pipeline.step import StrategyRuntimeBook
    from futures_trend_quant.core.models import Bar
    from futures_trend_quant.config import load_config,default_config_path
    import json
    rules=load_config(default_config_path()).raw['rules']
    bars=[]
    for n,c in enumerate([104,100,100,100,98]):
        end=NOW+timedelta(minutes=5*n)
        bars.append(Bar('DCE.m2701','300s',end-timedelta(minutes=5),end,c,c+1,c-1,c))
    monkeypatch.setattr(module,'calculate_indicators',lambda b:SimpleNamespace(ema34=[100]*len(b),atr14=[2]*len(b)))
    monkeypatch.setattr(module,'_latest_near_extreme_pattern',lambda b,*a:EvidenceEvent('pattern','bull',b[-1].identity,len(b)-1,b[-1].eob) if b[-1]==bars[2] else None)
    sessions={b.identity:'ACTUAL_SECTION_TEST' for b in bars}
    book=module.OrderedBBook();book.advance(bars[:3],'bull',sessions,rules,None)
    assert book.active['start_bar_id']==bars[1].identity and book.active['confirmation_bar_id']==bars[2].identity
    runtime=StrategyRuntimeBook(ordered_b={'DCE.M':book});restored=StrategyRuntimeBook.from_dict(json.loads(json.dumps(runtime.to_dict())))
    restored.ordered_b['DCE.M'].advance(bars[:4],'bull',sessions,rules,None)
    assert restored.ordered_b['DCE.M'].active['start_bar_id']==bars[1].identity
    assert book.last_eob==bars[2].eob.isoformat()
    candidate=restored.ordered_b['DCE.M'].candidate(bars[:4]);restored.record_fill(csymbol='DCE.M',direction='bull',price=101,evidence_id=candidate.evidence_id)
    assert restored.ordered_b['DCE.M'].active is None
    book.advance(bars,'bull',sessions,rules,None)
    assert book.active is None and book.tombstones[-1]['reason']=='START_CLOSE_BROKEN'
    missing=module.OrderedBBook();missing.advance(bars[:2],'bull',sessions,rules,None)
    gap=replace(bars[3],bob=bars[3].bob+timedelta(minutes=5),eob=bars[3].eob+timedelta(minutes=5))
    missing.advance([gap],'bull',{gap.identity:'ACTUAL_SECTION_TEST'},rules,None)
    assert missing.tombstones[-1]['reason']=='MISSING_M5_INSIDE_SESSION'


def test_real_session_ids_accept_equivalent_fixed_offset_and_named_timezone():
    import pandas as pd
    from types import SimpleNamespace
    from test_paper_execution import calendar
    c=calendar()
    bar=SimpleNamespace(bob=NOW.to_pydatetime(),eob=(NOW+timedelta(minutes=5)).tz_convert('Asia/Shanghai'))
    assert c.section_for_bar(bar)
    later=SimpleNamespace(bob=pd.Timestamp('2026-11-05T10:30:00+08:00'),eob=pd.Timestamp('2026-11-05T10:35:00+08:00'))
    assert c.section_for_bar(bar)!=c.section_for_bar(later)
