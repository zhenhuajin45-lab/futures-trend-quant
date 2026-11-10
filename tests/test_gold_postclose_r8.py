"""R8 causal lifecycle, migration and production-isolation regressions."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest
from helpers import make_bars
from futures_trend_quant.core.models import TrendState
from futures_trend_quant.core.facts_state import FactsState, StablePivot, StableDivergence, divergence_event_id, latest_valid_divergence
from futures_trend_quant.core.trend.divergence_shadow import advance
from futures_trend_quant.core.trend.state_machine import TrendFacts, step_trend
from futures_trend_quant.core.pipeline import CoreStepInput, StrategyRuntimeBook, run_core_step
from futures_trend_quant.core.pipeline.permissions import evaluate_permission, POLICY
from futures_trend_quant.config import load_config, default_config_path

SYMBOLS=['SHFE.au2612','DCE.m2701','CZCE.OI701','DCE.y2701','DCE.p2701']


def identity(symbol, n):
    # Friday night through Monday: no age-based expiry over the weekend.
    start=datetime(2026,11,6,21,tzinfo=timezone(timedelta(hours=8)))
    when=start+timedelta(minutes=15*n) if n<8 else start+timedelta(days=3,minutes=15*n)
    return symbol+'|900s|'+when.isoformat()


def fact(symbol,n,direction):
    return TrendFacts(identity(symbol,n),100,104,96,'above' if direction=='bull' else 'below',
                      'up' if direction=='bull' else 'down',True,True)


def event(symbol,kind,n=0):
    return StableDivergence(kind,'B_hist',identity(symbol,n-2),identity(symbol,n-1),identity(symbol,n),3,('hist',),110 if kind=='top' else 90)


def pivots(symbol,direction):
    values=[('low',95),('high',100),('low',96),('high',102)] if direction=='bull' else [('high',105),('low',100),('high',104),('low',98)]
    return [StablePivot(identity(symbol,n),identity(symbol,n+1),k,p,1) for n,(k,p) in zip([2,4,6,8],values)]


@pytest.mark.parametrize('symbol',SYMBOLS)
@pytest.mark.parametrize('direction',['bull','bear'])
def test_dual_divergence_replacement_is_causal_mirrored_persistent_and_shadow_only(symbol,direction):
    fs=FactsState(divergences=[event(symbol,'top'),event(symbol,'bottom')]);book={};prior=TrendState()
    advance(book,fs,prior,fact(symbol,1,direction));fs.pivots=pivots(symbol,direction)
    before=deepcopy(fs.to_dict());production=step_trend(prior,fact(symbol,9,direction))
    shadow=advance(book,fs,prior,fact(symbol,9,direction))
    assert production.state.trend_state=='none' and shadow['decision']['state']['establish_votes']==1
    key=divergence_event_id(fs.divergences[0 if direction=='bull' else 1])
    assert shadow['events'][key]['structural_valid'] and not shadow['events'][key]['warning_active']
    assert shadow['events'][key]['replacement']['actual_known_at'] is None
    assert shadow['affects_execution'] is False and fs.to_dict()==before
    restored=json.loads(json.dumps(book));fs.pivots=[]  # left edge rolled beyond proof
    second=advance(restored,fs,prior,fact(symbol,10,direction))
    assert second['decision']['confirmed_state']==direction and key in restored['consumed']
    fs.divergences.append(event(symbol,'top' if direction=='bull' else 'bottom',11))
    third=advance(restored,fs,prior,fact(symbol,12,direction))
    assert third['events'][divergence_event_id(fs.divergences[-1])]['entry_veto_active']
    assert key in restored['consumed']
    with pytest.raises(ValueError,match='late or revised'):advance(restored,fs,prior,fact(symbol,12,direction))
    with pytest.raises(ValueError,match='late or revised'):advance(restored,fs,prior,fact(symbol,8,direction))


@pytest.mark.parametrize('mutation',['equal','future','foreign','nonalternating','before_event'])
def test_unconfirmed_equal_or_unrelated_structure_never_consumes(mutation):
    symbol=SYMBOLS[0];fs=FactsState(divergences=[event(symbol,'top')]);book={}
    advance(book,fs,TrendState(),fact(symbol,1,'bull'));ps=pivots(symbol,'bull')
    if mutation=='equal':ps[-1]=replace(ps[-1],price=100)
    if mutation=='future':ps[-1]=replace(ps[-1],defined_at_bar_id=identity(symbol,20))
    if mutation=='foreign':ps[-1]=replace(ps[-1],bar_id=identity(SYMBOLS[1],8))
    if mutation=='nonalternating':ps[-2]=replace(ps[-2],kind='high')
    if mutation=='before_event':fs.divergences=[event(symbol,'top',7)]
    fs.pivots=ps;advance(book,fs,TrendState(),fact(symbol,10,'bull'))
    assert not book['consumed']


@pytest.mark.parametrize('kind',['top','bottom'])
def test_extreme_equality_and_break_tombstone_are_untouched(kind):
    symbol=SYMBOLS[0];ev=event(symbol,kind);fs=FactsState(divergences=[ev]);bar=make_bars(1,symbol=symbol)[0]
    bar=replace(bar,bob=datetime.fromisoformat(identity(symbol,1).rsplit('|',1)[-1]),eob=datetime.fromisoformat(identity(symbol,2).rsplit('|',1)[-1]),high=110,low=90)
    assert latest_valid_divergence(fs,[bar],kind)==ev
    broken=replace(bar,high=110.01) if kind=='top' else replace(bar,low=89.99)
    assert latest_valid_divergence(fs,[broken],kind) is None
    restored=FactsState.from_dict(fs.to_dict());assert latest_valid_divergence(restored,[],kind) is None


@pytest.mark.parametrize('side,slope,hist',[('above','up',1),('below','down',-1),('unavailable','flat',None)])
def test_none_does_not_cascade_assumed_bear_reasons(side,slope,hist):
    p=evaluate_permission('none',close_side=side,slope=slope,hist=hist,opposite_divergence=True,
        closed_h1_known=False,calendar_qualified=False,previous_weak=10,changed=True,eob='NOW')
    assert p.policy==POLICY and p.reasons==('NO_DIRECTION_AUTHORIZATION',)
    assert not p.new_entry_allowed and p.technical_exit_direction is None and p.reverse_authorized is None


@pytest.mark.parametrize('has_gap',[False,True])
def test_pa_gap_diagnostic_retains_exact_keys_without_claiming_historical_clocks(has_gap):
    from types import SimpleNamespace
    from futures_trend_quant.observation.price_action.coverage import describe
    bars=make_bars(4)
    retained=[b for i,b in enumerate(bars) if not (has_gap and i==2)]
    result=describe(retained,SimpleNamespace(clock=[b.eob for b in bars]),{b.identity:'CURRENT_RECEIPT' for b in retained})
    assert result['missing_count']==int(has_gap)
    assert result['missing_eob']==([bars[2].eob.isoformat()] if has_gap else [])
    assert result['historical_first_received_at']=='UNKNOWN' and result['receipt_missing_count']==0


@pytest.mark.parametrize('gap',['old','inside','leading'])
def test_pa_coverage_uses_consumer_horizon_and_keeps_old_gap_separate(gap):
    from types import SimpleNamespace
    from futures_trend_quant.observation.price_action.coverage import snapshot_coverage
    bars=make_bars(8);bad={'old':1,'inside':5,'leading':3}[gap]
    retained=[b for i,b in enumerate(bars) if i!=bad]
    result=snapshot_coverage(retained,SimpleNamespace(clock=[b.eob for b in bars]),
        {b.identity:'CURRENT_RECEIPT' for b in retained},bars[3:],[])
    assert result['retained_prefix']['missing_count']==1
    assert result['missing_count']==int(gap!='old')
    assert (result['status']=='COMPLETE_CLOSED_PREFIX')==(gap=='old')
    assert result['historical_first_received_at']=='UNKNOWN'


def test_pa_v1_migration_preserves_known_at_and_old_events(tmp_path):
    from futures_trend_quant.observation.price_action.state import State
    from futures_trend_quant.observation.price_action.models import POLICY, DEFAULTS, digest
    state=State(tmp_path,digest(DEFAULTS));state.data.update(policy='PRICE_ACTION_SHADOW_V1_20261110',
        products={'GOLD':{'pivots':{'OLD':{'known_at':'REAL_OLD_CLOCK'}}}},emitted={'OLD':'ORIGINAL'})
    state.save();old=deepcopy(state.data)
    new=State(tmp_path,digest(DEFAULTS));new.save()
    assert new.data['policy']==POLICY and new.data['products']==old['products'] and new.data['emitted']==old['emitted']
    assert new.data['migrations'][0]['source_state_sha256']==digest(old)
    with pytest.raises(ValueError,match='IDENTITY'):State(tmp_path,'UNKNOWN_PARAMETERS')


@pytest.mark.parametrize('symbol',SYMBOLS)
def test_full_core_migrates_old_state_and_shadow_cannot_change_production_result(symbol):
    bars=tuple(make_bars(300,step=.02,symbol=symbol));rules=load_config(default_config_path()).raw['rules']
    event_=CoreStepInput(symbol.split('.')[0]+'.TEST',symbol,bars,bars,bars,rules)
    old=StrategyRuntimeBook().to_dict();old.pop('divergence_shadow');old.pop('behavior_policy')
    left=StrategyRuntimeBook.from_dict(old);right=StrategyRuntimeBook.from_dict(old)
    baseline=run_core_step(event_,left)
    shadow=run_core_step(replace(event_,rules=dict(rules,divergence_structure_shadow_v1=True)),right)
    assert baseline.trend==shadow.trend and baseline.entry==shadow.entry and baseline.permission==shadow.permission
    assert shadow.facts_row['divergence_structure_shadow']['mode']=='SHADOW'
    assert not shadow.facts_row['divergence_structure_shadow']['affects_execution']
    assert StrategyRuntimeBook.from_dict(right.to_dict()).to_dict()==right.to_dict()


def test_shadow_exception_cannot_veto_production_or_commit_partial_consumption(monkeypatch):
    from futures_trend_quant.core.trend import divergence_shadow as module
    bars=tuple(make_bars(300,step=.02));rules=load_config(default_config_path()).raw['rules']
    event_=CoreStepInput('SHFE.AU',bars[-1].symbol,bars,bars,bars,rules)
    expected=run_core_step(event_,StrategyRuntimeBook())
    state=StrategyRuntimeBook()
    def broken(book,*args,**kwargs):
        book['consumed']={'FAKE':'PARTIAL'};raise ValueError('synthetic failure')
    monkeypatch.setattr(module,'advance',broken)
    got=run_core_step(replace(event_,rules=dict(rules,divergence_structure_shadow_v1=True)),state)
    assert got.trend==expected.trend and got.entry==expected.entry and got.permission==expected.permission
    assert got.facts_row['divergence_structure_shadow']['status']=='UNKNOWN'
    assert 'consumed' not in state.divergence_shadow['SHFE.AU']
    assert state.divergence_shadow['SHFE.AU']['diagnostic']['code']=='SHADOW_GAP_REQUIRES_REVIEW'
    corrupt=StrategyRuntimeBook();corrupt.divergence_shadow['SHFE.AU']=None
    got=run_core_step(replace(event_,rules=dict(rules,divergence_structure_shadow_v1=True)),corrupt)
    assert got.trend==expected.trend and got.entry==expected.entry
    assert corrupt.divergence_shadow['SHFE.AU']['unparsed_state'] is None
