"""Forward-only episodes against boundaries known before the bar."""
from .models import clock, digest

TERMINAL = {'CLOSED_BACK', 'FAILED_ACCEPTANCE', 'RETEST_HELD', 'INVALIDATED_GAP'}


def advance(zone, bar, tick, prior=None, known_at=None):
    if not zone.known_at or clock(zone.known_at) >= bar.eob:
        return dict(status='UNKNOWN', reason='BOUNDARY_NOT_KNOWN_BEFORE_TRIGGER')
    state = dict(prior or {})
    if state.get('last_bar_id') == bar.identity or state.get('status') in TERMINAL:
        return state
    direction = 1 if zone.role == 'RESISTANCE' else -1
    outside_wick = bar.high > zone.upper if direction == 1 else bar.low < zone.lower
    accepted = bar.close >= zone.upper+tick if direction == 1 else bar.close <= zone.lower-tick
    back = bar.close <= zone.upper if direction == 1 else bar.close >= zone.lower
    if not state:
        state = dict(status='KNOWN_ZONE', zone_id=zone.zone_id, episode_id=None, acceptance_bars=0)
    old = state['status']
    if old == 'KNOWN_ZONE' and outside_wick:
        state.update(episode_id=digest([zone.zone_id, bar.identity]), probe_bar_id=bar.identity,
                     occurred_at=bar.eob.isoformat(), status='CLOSE_ACCEPTED' if accepted else 'CLOSED_BACK' if back else 'PROBE_OUTSIDE')
    elif old == 'PROBE_OUTSIDE':
        if accepted:state['status'] = 'CLOSE_ACCEPTED'
        elif back:state['status'] = 'CLOSED_BACK'
    elif old in ('CLOSE_ACCEPTED', 'RETEST_PENDING'):
        if back:
            state['status'] = 'FAILED_ACCEPTANCE'
        elif (bar.low <= zone.upper if direction == 1 else bar.high >= zone.lower) and accepted:
            state['status'] = 'RETEST_HELD'
        else:state['status'] = 'RETEST_PENDING'
    if accepted:state['acceptance_bars'] += 1
    state.update(last_bar_id=bar.identity, confirmed_at=bar.eob.isoformat(), known_at=known_at,
                 signal_generated=False, role_change=('SUPPORT' if direction == 1 else 'RESISTANCE') if state['status']=='RETEST_HELD' else None)
    if state['status'] in TERMINAL:state['terminal_at'] = known_at
    return state
