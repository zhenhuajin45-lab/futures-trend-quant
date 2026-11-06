"""Incident regressions. Historical bad output remains pinned at dfed9d6a."""
import pytest
from datetime import datetime,timedelta,timezone
from futures_trend_quant.core.models import Bar,Pivot,TrendState
from futures_trend_quant.core.entry.anchor import AnchorSelection,select_anchor
from futures_trend_quant.core.risk import build_stop_plan
from futures_trend_quant.core.entry import EntryContext,EntrySettings,EvidenceSnapshot,EvidenceEvent,evaluate_entries
from futures_trend_quant.core.trend.state_machine import TrendFacts,step_trend


def test_old_anchor_missing_from_confirmed_pivots_is_rejected():
    start=datetime(2026,11,1,9,tzinfo=timezone(timedelta(hours=8)));values=[111,106,97,104,100]
    bars=[Bar('SHFE.au2612','900s',start+timedelta(minutes=15*i),start+timedelta(minutes=15*(i+1)),v,v+1,v-1,v) for i,v in enumerate(values)]
    previous=AnchorSelection('bull','swing',0,110,anchor_bar_id=bars[0].identity)
    pivots=(Pivot(2,3,'low',96,2),Pivot(3,4,'high',105,2))
    kept=select_anchor(bars,'bull',pivots=pivots,previous=previous)
    fresh=select_anchor(bars,'bull',pivots=pivots)
    assert kept is None
    assert fresh.anchor_price==96 and any(b.identity==previous.anchor_bar_id for b in bars)


def test_wrong_side_structural_anchor_cannot_produce_a_stop():
    with pytest.raises(ValueError,match='INVALID_ANCHOR_SIDE'):
        build_stop_plan(direction='bull',entry=886.72,anchor=934.44,atr=2.5257105205353425,tick=.02)


def test_path_b_can_accept_while_m15_reverse_divergence_and_momentum_gate_are_adverse():
    c=EntryContext(direction='bull',trend_confirmed=True,h1_state='bull_strong',m15_slope_state='up',m15_reverse_divergence=True,gate_open=False,m15_index=20,current_price=100,atr14=2)
    e=EvidenceSnapshot(direction='bull',m5_pullback=EvidenceEvent('pullback','bull','P',10),m5_pattern=EvidenceEvent('pattern','bull','Q',11))
    s=EntrySettings(path_a_enabled=False,path_b_enabled=True,path_c_enabled=False,path_d_enabled=False,cooldown_enabled=False)
    result=evaluate_entries(c,e,s)
    assert result.accepted and result.path=='B'


def test_h1_strong_veto_preserves_bull_even_with_top_divergence_pattern_and_below_ema():
    state=TrendState(trend_state='bull',trend_extreme=110)
    f=TrendFacts(bar_id='closed-m15',close=99,high=103,low=98,close_side='below',slope_state='up',top_divergence=True,s2_bearish_pattern=True,p_close_cross=True,h1_state='strong')
    r=step_trend(state,f)
    assert r.proposed_action=='veto' and r.confirmed_state=='bull' and r.state.leave_votes==0
