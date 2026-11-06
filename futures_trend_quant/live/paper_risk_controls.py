"""Versioned execution controls; shadow observations never mutate exits."""
from datetime import timedelta
import math
import pandas as pd

VERSION = 'POSTCLOSE_EXEC_V3_20261106'


def enabled(config):
    return config.get('execution_policy_version') == VERSION


def cooldown_reason(state, now, calendar):
    risk = state.get('loss_cooldown', {})
    if risk.get('until') and pd.Timestamp(now) < pd.Timestamp(risk['until']):
        return 'CONSECUTIVE_LOSS_COOLDOWN'
    if risk.get('blocked_through_day') and calendar.day(now) <= risk['blocked_through_day']:
        return 'CONSECUTIVE_LOSS_NEXT_TRADING_DAY'
    return None


def update_roundtrips(state, now, calendar):
    """Only confirmed flat, fully fee-evidenced strategy roundtrips count once."""
    ledger = state.setdefault('strategy_roundtrips', {})
    pending = []
    for close in state['intents'].values():
        if close['effect'] == 1 or not close.get('closed_confirmed') or close.get('tag') != 'STRATEGY':
            continue
        opening = state['intents'].get(close.get('open_intent_id'))
        if opening is None:
            matches = [i for i in state['intents'].values() if i['effect'] == 1 and i.get('filled') == 1
                       and i.get('tag') == 'STRATEGY' and i['symbol'] == close['symbol']
                       and i['direction'] == close['direction'] and i.get('filled_vwap') == close.get('entry_price')
                       and i.get('broker_updated_at') == close.get('opened_at')]
            if len(matches) == 1:
                opening = matches[0]
                close['open_intent_id'] = opening['id']
        if opening is None:
            raise ValueError('completed strategy close has ambiguous opening identity')
        key = opening['id']
        if key in ledger:
            continue
        fills = state.get('execution_fees', {})
        groups = [[f for f in fills.values() if f.get('cl_ord_id') == i.get('order_id')]
                  for i in (opening, close)]
        if any(sum(float(f.get('volume', 0)) for f in group) != 1 for group in groups):
            raise ValueError('completed strategy roundtrip fee/volume evidence pending')
        fees = sum(float(f['commission']) for group in groups for f in group)
        if not math.isfinite(fees) or fees < 0:
            raise ValueError('roundtrip platform commission unknown')
        gross = (float(close['filled_vwap']) - float(opening['filled_vwap'])) * (
            1 if opening['direction'] == 'bull' else -1) * float(opening['multiplier'])
        end = close.get('broker_updated_at') or close.get('terminal_received_at')
        if not end:
            raise ValueError('roundtrip completion clock unknown')
        pending.append((pd.Timestamp(end), key, dict(open_intent_id=key, close_intent_id=close['id'],
                       closed_at=end, gross=gross, platform_commission=fees, platform_net=gross-fees,
                       fee_scope='PLATFORM_SIMULATED_NOT_CERTIFIED_REAL_COST')))
    risk = state.setdefault('loss_cooldown', dict(consecutive_losses=0))
    for end, key, row in sorted(pending):
        if risk.get('last_closed_at') and end < pd.Timestamp(risk['last_closed_at']):
            raise ValueError('late prior roundtrip requires chronological risk reconciliation')
        net = row['platform_net']
        streak = risk['consecutive_losses'] + 1 if net < 0 else 0 if net > 0 else risk['consecutive_losses']
        risk.update(consecutive_losses=streak, last_closed_at=row['closed_at'])
        if net < 0 and streak >= 3:
            # Late reconciliation cannot shorten the required observed cooldown.
            until = max(pd.Timestamp(now), end) + timedelta(hours=1)
            if risk.get('until'):
                until = max(until, pd.Timestamp(risk['until']))
            risk['until'] = until.isoformat()
            if streak >= 5:
                risk['blocked_through_day'] = calendar.day(now)
        ledger[key] = row
    return [row for _, _, row in sorted(pending)]


def observe_shadow(meta, bar, now, calendar):
    """Use only fully post-fill closed M1; fixed original R/ATR, no execution."""
    if meta.get('tag') != 'STRATEGY' or pd.Timestamp(bar.bob) < pd.Timestamp(meta['opened_at']):
        return None
    entry = float(meta['entry_price']); initial_stop = float(meta['stop'])
    risk = abs(entry-initial_stop); atr = float(meta.get('entry_atr') or 0)
    if risk <= 0 or atr <= 0:
        return None
    s = meta.setdefault('shadow_risk', dict(initial_r=risk, entry_atr=atr, favorable_r=0.,
                         observed_minutes=[], complete_m15=0, trailing_stop=None, time_stop_triggered=False))
    identity = str(bar.eob)
    if identity in s['observed_minutes']:
        return None
    s['observed_minutes'].append(identity)
    sign = 1 if meta['direction'] == 'bull' else -1
    favorable = float(bar.high if sign == 1 else bar.low)
    s['favorable_r'] = max(s['favorable_r'], sign*(favorable-entry)/s['initial_r'])
    # Count complete calendar M15 buckets, never replace missing minutes with wall time.
    # Exclude the partial entry bucket. Preselect exactly the first three legal
    # complete buckets, so a data hole cannot move the test to a nicer later window.
    start=pd.Timestamp(meta['opened_at']).ceil('15min')
    expected = calendar.clock[calendar.clock > start][:180]
    actual = {pd.Timestamp(x) for x in s['observed_minutes']}
    buckets = {}
    for minute in expected:
        buckets.setdefault(minute.ceil('15min'), []).append(minute)
    first_three=[(end,minutes) for end,minutes in sorted(buckets.items()) if len(minutes)==15][:3]
    s['complete_m15'] = sum(all(m in actual for m in minutes) for end,minutes in first_three)
    result = None
    if len(first_three)==3 and pd.Timestamp(bar.eob)>=first_three[-1][0] and not s.get('time_stop_evaluated'):
        s['time_stop_evaluated'] = True
        s['time_stop_triggered'] = s['favorable_r'] < .5 if s['complete_m15']==3 else None
        s['time_stop_status'] = 'OBSERVED' if s['complete_m15']==3 else 'INSUFFICIENT_MINUTE_COVERAGE'
        s['time_stop_at'] = str(first_three[-1][0])
        result = dict(kind='SHADOW_TIME_STOP', triggered=s['time_stop_triggered'],status=s['time_stop_status'])
    best = entry + sign*s['favorable_r']*s['initial_r']
    distance = (1.1 if sign*(best-entry) >= 3.5*atr else 1.7)*atr
    proposed = best-sign*distance
    tick = float(meta.get('entry_tick') or 0)
    if tick > 0 and sign*(best-entry) >= 1.1*atr:
        proposed = max(proposed, entry+2*tick) if sign == 1 else min(proposed, entry-2*tick)
    old = s['trailing_stop']
    s['trailing_stop'] = proposed if old is None else max(old, proposed) if sign == 1 else min(old, proposed)
    s['at'] = str(now)
    return result
