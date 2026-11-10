"""Causal structure replacement experiment. Never grants production permission."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime

from ..facts_state import divergence_event_id
from ..models import TrendState
from .state_machine import step_trend

POLICY = 'DIVERGENCE_STRUCTURE_REPLACEMENT_SHADOW_V1_20261111'


def clock(bar_id):
    return datetime.fromisoformat(bar_id.rsplit('|', 1)[-1])


def replacement(pivots, event, bar_id, floor):
    """Four alternating confirmed pivots, all formed after this divergence."""
    symbol, frequency = bar_id.split('|')[:2]
    cutoff = clock(bar_id)
    selected = [p for p in pivots
                if p.bar_id.split('|')[:2] == [symbol, frequency]
                and p.defined_at_bar_id.split('|')[:2] == [symbol, frequency]
                and clock(event.defined_at_bar_id) < clock(p.bar_id)
                and clock(floor) < clock(p.defined_at_bar_id) <= cutoff
                and clock(p.bar_id) <= clock(p.defined_at_bar_id)]
    selected.sort(key=lambda p: clock(p.bar_id))
    four = selected[-4:]
    if len(four) != 4 or any(a.kind == b.kind for a, b in zip(four, four[1:])):
        return None
    highs = [p for p in four if p.kind == 'high']
    lows = [p for p in four if p.kind == 'low']
    if len(highs) != 2 or len(lows) != 2:
        return None
    bull = highs[1].price > highs[0].price and lows[1].price > lows[0].price
    bear = highs[1].price < highs[0].price and lows[1].price < lows[0].price
    if not (bull if event.kind == 'top' else bear):
        return None
    return dict(policy=POLICY, direction='bull' if bull else 'bear',
                source_ids=[p.bar_id for p in four],
                rule_confirmed_at_bar_id=max(four, key=lambda p: clock(p.defined_at_bar_id)).defined_at_bar_id,
                consumed_at_bar_id=bar_id, actual_known_at=None,
                known_time_quality='UNKNOWN_HISTORICAL_RECEIVE_CLOCK',
                reason='CONFIRMED_HH_HL' if bull else 'CONFIRMED_LH_LL')


def advance(book, facts_state, prior, facts, *, veto_enabled=True):
    """Mutate only the independent shadow book. Production facts stay intact."""
    if not book:
        book.update(policy=POLICY, migration_floor=facts.bar_id, consumed={},
                    trend=prior.to_dict(), last_bar_id=None, events={})
    if book.get('policy') != POLICY:
        raise ValueError('unregistered divergence shadow policy')
    if book.get('last_bar_id') and clock(facts.bar_id) <= clock(book['last_bar_id']):
        raise ValueError('shadow late or revised closed bar')
    events = {}
    latest = {}
    for event in facts_state.divergences:
        if clock(event.defined_at_bar_id) > clock(facts.bar_id):
            continue
        if event.current_bar_id.split('|')[:2] != facts.bar_id.split('|')[:2]:
            continue
        key = divergence_event_id(event)
        valid = key not in facts_state.invalidated
        if valid and key not in book['consumed']:
            proof = replacement(facts_state.pivots, event, facts.bar_id, book['migration_floor'])
            if proof:
                book['consumed'][key] = proof
        active = valid and key not in book['consumed']
        events[key] = dict(structural_valid=valid, warning_active=active,
                           entry_veto_active=active, production_veto_unchanged=True,
                           defined_at_bar_id=event.defined_at_bar_id,
                           extreme_price=event.extreme_price,
                           invalidation=deepcopy(facts_state.invalidated.get(key)),
                           replacement=deepcopy(book['consumed'].get(key)))
        latest[event.kind] = active
    shadow_facts = replace(facts, top_divergence=latest.get('top', False),
                           bottom_divergence=latest.get('bottom', False))
    decision = step_trend(TrendState.from_dict(book['trend']), shadow_facts, veto_enabled=veto_enabled)
    book.update(trend=decision.state.to_dict(), last_bar_id=facts.bar_id, events=events)
    return dict(policy=POLICY, mode='SHADOW', affects_execution=False,
                migration_floor=book['migration_floor'], events=deepcopy(events),
                decision=decision.audit(), actual_known_at=None,
                production_direction=prior.trend_state,
                path_qualification='NOT_EVALUATED', executable='NOT_EVALUATED')
