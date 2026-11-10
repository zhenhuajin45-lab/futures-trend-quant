"""Reuse S_A pivots; only their confirmation prefix can define structure."""
from .models import PivotFact, clock, digest


def confirmed_pivots(bars, received, asof, k=2, leg_min_atr=1.2):
    from futures_trend_quant.core.facts.indicators import atr
    from futures_trend_quant.core.facts.swing import find_pivots
    prefix = [b for b in bars if b.eob <= clock(asof)]
    result = []
    for p in find_pivots(prefix, atr(prefix), k=k, leg_min_atr=leg_min_atr, mode='S_A'):
        born, confirmed = prefix[p.index], prefix[p.defined_at]
        clocks = [received.get(b.identity) for b in prefix[max(0, p.index-k):p.defined_at+1]]
        known = None if any(t is None for t in clocks) else max(map(clock, clocks)).isoformat()
        if known is not None and clock(known) > clock(asof):
            continue
        result.append(PivotFact(digest([born.identity, confirmed.identity, p.kind]), born.identity,
                                p.kind, p.price, born.eob.isoformat(), confirmed.eob.isoformat(), known, p.atr))
    return result


def relation(a, b, tick):
    # Integer tick comparison with floating-point representation noise removed.
    x, y = round(a/tick), round(b/tick)
    return 'HIGHER' if y > x else 'LOWER' if y < x else 'EQUAL'


def structure(pivots, close, tick):
    if tick <= 0:
        raise ValueError('PA_INVALID_TICK')
    known = [p for p in pivots if p.known_at is not None]
    highs = [p for p in known if p.kind == 'high']
    lows = [p for p in known if p.kind == 'low']
    if len(highs) < 2 or len(lows) < 2:
        return dict(structure_state='UNKNOWN', reason_codes=['INSUFFICIENT_CONFIRMED_PIVOTS'], protected_pivot=None)
    hr = relation(highs[-2].price, highs[-1].price, tick)
    lr = relation(lows[-2].price, lows[-1].price, tick)
    state = 'UP' if hr == lr == 'HIGHER' else 'DOWN' if hr == lr == 'LOWER' else 'TRANSITION'
    protected = lows[-1] if state == 'UP' else highs[-1] if state == 'DOWN' else None
    damaged = bool(protected and (close < protected.price if state == 'UP' else close > protected.price))
    return dict(structure_state=state+'_DAMAGED' if damaged else state, high_relation=hr, low_relation=lr,
                protected_pivot=None if protected is None else protected.row(),
                damage_event='CLOSED_BREAK' if damaged else None,
                next_confirmation_required='INDEPENDENT_OPPOSITE_HIGH_AND_LOW' if damaged else None,
                reason_codes=['EQUAL_PIVOT'] if 'EQUAL' in (hr, lr) else [])
