"""Immutable born-width zones, independent touches and preregistered boxes."""
from .models import Zone, clock, digest


def pivot_zone(pivot, tick, params):
    width = max(params['zone_ticks']*tick, params['zone_atr']*pivot.atr)
    return Zone(digest(['PIVOT', pivot.event_id, width]), 'M15_PIVOT',
                'RESISTANCE' if pivot.kind == 'high' else 'SUPPORT', pivot.price-width,
                pivot.price+width, (pivot.event_id,), pivot.confirmed_at, pivot.known_at)


def previous_day_zones(bars, day_by_id, complete_days, asof_day, tick, atr_value, known_at, params):
    """Complete days must come from explicit exchange coverage, never date midnight."""
    prior = sorted(d for d in complete_days if d < asof_day)
    if not prior:
        return []
    day = prior[-1]
    members = [b for b in bars if day_by_id.get(b.identity) == day]
    if not members:
        return []
    width = max(tick*params['zone_ticks'], atr_value*params['zone_atr'])
    return [Zone(digest(['PREVIOUS_TRADING_DAY', day, role, price, width]), 'PREVIOUS_TRADING_DAY',
                 role, price-width, price+width, tuple(b.identity for b in members),
                 members[-1].eob.isoformat(), known_at)
            for role, price in [('SUPPORT', min(b.low for b in members)),
                                ('RESISTANCE', max(b.high for b in members))]]


def mature_range(bars, pivots, asof, params):
    # Caller supplies only bars strictly BEFORE the current potential breakout.
    window = bars[-params['range_window']:]
    if len(window) < params['range_window']:
        return dict(status='UNKNOWN', reason='INSUFFICIENT_RANGE_WINDOW')
    eligible = [p for p in pivots if p.known_at and clock(p.known_at) <= clock(asof)
                and window[0].eob <= clock(p.occurred_at) <= window[-1].eob]
    highs = [p for p in eligible if p.kind == 'high']
    lows = [p for p in eligible if p.kind == 'low']
    n = params['range_touches']
    if len(highs) < n or len(lows) < n:
        return dict(status='NOT_FORMED', reason='INSUFFICIENT_DISTINCT_PIVOTS')
    # Cluster tolerance uses frozen pivot ATR; avoid classifying any mixed trend as RANGE.
    tolerance = max(p.atr for p in eligible)*params['zone_atr']*2
    hs, ls = highs[-n:], lows[-n:]
    if max(p.price for p in hs)-min(p.price for p in hs) > tolerance or max(p.price for p in ls)-min(p.price for p in ls) > tolerance:
        return dict(status='NOT_FORMED', reason='BOUNDARIES_NOT_CLUSTERED')
    low, high = min(p.price for p in ls), max(p.price for p in hs)
    if high <= low:
        return dict(status='UNKNOWN', reason='INVALID_BOX')
    return dict(status='FORMED', lower=low, upper=high,
                range_id=digest(sorted(p.event_id for p in hs+ls)), source_ids=[p.event_id for p in hs+ls],
                known_at=max((p.known_at for p in hs+ls),key=clock), born_at=window[-1].eob.isoformat())


def short_box(bars, atr_value, params):
    from futures_trend_quant.core.facts.indicators import calculate_indicators
    sample = bars[-params['short_box_bars']:]
    if len(sample) < params['short_box_bars'] or not atr_value or atr_value <= 0:
        return dict(status='UNKNOWN')
    if any((b.eob-a.eob).total_seconds()!=300 for a,b in zip(sample,sample[1:])):
        return dict(status='UNKNOWN',reason='SHORT_BOX_SECTION_GAP')
    indicators=calculate_indicators(bars)
    m5_atr=indicators.atr14[-1]
    emas=indicators.ema34[-params['short_box_bars']:]
    if m5_atr is None or any(e is None for e in emas):
        return dict(status='UNKNOWN',reason='ORIGINAL_C_M5_INDICATORS_INCOMPLETE')
    side='bull' if all(b.close>e for b,e in zip(sample,emas)) else 'bear' if all(b.close<e for b,e in zip(sample,emas)) else None
    low, high = min(b.low for b in sample), max(b.high for b in sample)
    return dict(status='FORMED' if side and 0 < high-low <= params['short_box_max_atr']*m5_atr else 'NOT_FORMED',
                lower=low, upper=high, direction=side,m5_atr=m5_atr,threshold_atr=params['short_box_max_atr'],
                definition='ORIGINAL_C_FOUR_M5_SAME_EMA_SIDE_AND_M5_ATR',source_ids=[b.identity for b in sample])


def position(price, box, params):
    if box.get('status') != 'FORMED' or box['upper'] <= box['lower']:
        return dict(location_class='UNKNOWN', position=None)
    x = (price-box['lower'])/(box['upper']-box['lower'])
    label = 'BELOW' if x < 0 else 'ABOVE' if x > 1 else 'MIDDLE' if params['middle_low'] <= x <= params['middle_high'] else 'LOWER_EDGE' if x < params['middle_low'] else 'UPPER_EDGE'
    return dict(location_class=label, position=x)


def touch(zone, bar, prior):
    inside = bar.high >= zone.lower and bar.low <= zone.upper
    return dict(inside=inside, count=int(prior.get('count', 0))+(inside and not prior.get('inside', False)),
                last_touch=bar.identity if inside else prior.get('last_touch'))
