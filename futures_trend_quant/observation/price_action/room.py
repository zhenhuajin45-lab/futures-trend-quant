import math
from .models import clock


def available_room(direction, entry, stop, multiplier, zones, asof, *, coverage=False,
                   quote_known=False, costs=None):
    unknown = dict(status='UNKNOWN', rr_space_net=None, obstacle=None, reason_codes=[])
    if direction not in ('bull', 'bear') or any(not isinstance(x,(float,int)) or not math.isfinite(x) or x<=0 for x in (entry,stop,multiplier)) or entry==stop:
        return dict(unknown, reason_codes=['INVALID_OR_MISSING_PLAN'])
    if not (stop<entry if direction=='bull' else stop>entry):
        return dict(unknown,reason_codes=['INVALID_STOP_SIDE'])
    if not coverage or not quote_known:
        return dict(unknown, reason_codes=['ZONE_COVERAGE_OR_EXECUTABLE_QUOTE_UNKNOWN'])
    role = 'RESISTANCE' if direction == 'bull' else 'SUPPORT'
    eligible = [z for z in zones if z.role==role and z.known_at and clock(z.known_at)<=clock(asof)
                and (z.upper>=entry if direction=='bull' else z.lower<=entry)]
    if not eligible:
        return dict(unknown, status='NO_OBSTACLE_OBSERVED', reason_codes=['NOT_INFINITE_SPACE_OR_PASS'])
    distance = lambda z: max(0, z.lower-entry if direction=='bull' else entry-z.upper)
    zone = min(eligible,key=distance)
    gross = distance(zone)*multiplier
    risk = abs(entry-stop)*multiplier
    result = dict(unknown, obstacle=zone.row(), distance=distance(zone), risk_gross=risk, space_gross=gross)
    if costs is None or any(type(costs.get(k)) not in (int,float) or not math.isfinite(costs[k]) or costs[k]<0 for k in ('win','loss')):
        return dict(result, reason_codes=['FEES_OR_SLIPPAGE_UNKNOWN'])
    if not costs.get('effective_contract_date') or costs.get('entry_spread_counted_twice') is not False:
        return dict(result, reason_codes=['COST_BASIS_UNKNOWN_OR_DOUBLE_COUNTED'])
    return dict(result, status='SHADOW_MEASURED', rr_space_net=(gross-costs['win'])/(risk+costs['loss']),
                costs=costs, reference_rr=2, execution_gate_changed=False)
