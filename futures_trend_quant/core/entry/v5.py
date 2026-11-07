"""Per-path eligibility before selection. All raw candidates receive fixed clocks."""
from dataclasses import replace,asdict
from datetime import timedelta,datetime
from math import isfinite
from .models import EntryDecision
from .paths import evaluate_entries,evaluate_path_a,evaluate_path_c,evaluate_path_d_candidate,entry_h1_reasons
from ..pipeline.permissions import evaluate_permission

POLICY='PAPER_ENTRY_V5'


def path_permission(path,background,**facts):
    p=evaluate_permission(background,**facts)
    if path!='B':return replace(p,policy=POLICY)
    long=background=='bull';hist=facts['hist']
    checks=[(background in ('bull','bear'),'NO_DIRECTION_AUTHORIZATION'),
        (facts['close_side']==('above' if long else 'below'),'M15_CLOSE_ADVERSE'),
        (facts['slope']==('up' if long else 'down'),'M15_SLOPE_NOT_ALIGNED'),
        (isinstance(hist,(int,float)) and isfinite(hist),'M15_MACD_UNKNOWN'),
        (not facts['opposite_divergence'],'M15_OPPOSITE_DIVERGENCE_ACTIVE'),
        (facts['closed_h1_known'],'CLOSED_H1_UNKNOWN'),(facts['calendar_qualified'],'H1_CALENDAR_UNQUALIFIED')]
    reasons=tuple(reason for passed,reason in checks if not passed)
    return replace(p,policy=POLICY,reasons=reasons,new_entry_allowed=not reasons)


def select(context,evidence,settings,ordered,*,facts,clocks,csymbol,eob):
    c_raw=evaluate_path_c(replace(context,same_direction_positions=0),evidence,settings)
    d_raw=evaluate_path_d_candidate(replace(context,h1_state=context.direction+'_strong',m15_slope_state='up' if context.direction=='bull' else 'down',m15_reverse_divergence=False,gate_open=True),replace(evidence,m1_strong_reverse=False),settings)
    raw=tuple(c for c in (evaluate_path_a(context,evidence,settings),ordered if settings.path_b_enabled else None,c_raw,d_raw) if c is not None)
    audit=[];eligible=[]
    for c in raw:
        if c.trigger_eob is None or c.trigger_eob.tzinfo is None:
            audit.append(dict(path=c.path,evidence_id=c.evidence_id,eligible=False,reasons=['CONFIRMATION_CLOCK_UNKNOWN']));continue
        clock=clocks.setdefault(csymbol+'|'+c.evidence_id,dict(evidence_created_at=c.trigger_eob.isoformat(),first_eligible_at=eob.isoformat(),first_evaluation_m15_eob=eob.isoformat(),expires_at=(eob+timedelta(seconds=30)).isoformat()))
        reasons=[]
        if eob>datetime.fromisoformat(clock['expires_at']):reasons.append('EVIDENCE_EXPIRED')
        if c.path=='B':
            if entry_h1_reasons(context.h1_state,context.direction,settings.path_b_h1_exception)[0]!='PASS':reasons.append('PATH_B_H1_REJECTED')
            if evidence.m5_reverse_divergence:reasons.append('M5_REVERSE_DIVERGENCE')
        if c.path=='C' and evaluate_path_c(context,evidence,settings) is None:reasons.append('PATH_C_PREREQUISITE')
        if c.path=='D' and evaluate_path_d_candidate(context,evidence,settings) is None:reasons.append('PATH_D_PREREQUISITE')
        base=evaluate_entries(context,evidence,settings,candidates_override=(c,))
        if not base.accepted:reasons.append(base.reason)
        p=path_permission(c.path,context.direction,**facts)
        reasons.extend(p.reasons)
        audit.append(dict(path=c.path,evidence_id=c.evidence_id,process_id=c.process_id,trigger_bar_id=c.trigger_bar_id,confirmation_eob=c.trigger_eob.isoformat(),clock=dict(clock),eligible=not reasons,reasons=reasons,hist=facts['hist'],hist_role='ADVISORY' if c.path=='B' else 'HARD_GATE'))
        if not reasons:eligible.append((c,base,p,clock))
    for path in 'ABCD':
        if not any(x['path']==path for x in audit):
            audit.append(dict(path=path,evidence_id=None,eligible=False,reasons=['PATH_DISABLED' if not getattr(settings,'path_'+path.lower()+'_enabled') else 'ORDERED_PROCESS_NOT_CONFIRMED_OR_ACTIVE' if path=='B' else 'NO_TECHNICAL_PATH']))
    if not eligible:return EntryDecision(False,context.direction,'NO_ELIGIBLE_PATH',candidates=raw),None,None,audit
    c,base,p,clock=max(eligible,key=lambda item:item[0].trigger_sequence)
    return replace(base,candidates=raw),p,clock,audit
