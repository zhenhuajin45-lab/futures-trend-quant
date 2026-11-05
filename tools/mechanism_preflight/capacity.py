"""Past-only capacity accounting. Every calendar slot is retained."""
import math
import pandas as pd


def capacity_row(r):
    complete = r.get('feature_complete') is True
    active = complete and r.get('direction') in (-1, 1)
    if 'adapted_window_compatible' not in r:
        window = 'UNKNOWN'
    elif not r.get('next_legal_model_entry_at'):
        window = 'NO_ENTRY_WINDOW'
    elif r['adapted_window_compatible'] is not True:
        window = 'WINDOW_MISMATCH'
    else:
        window = 'VALID'
    implementable = active and window == 'VALID'
    candidate = r.get('risk_valid') is True
    control = r.get('control_risk_valid') is True
    relation = ('SAME' if r['direction'] == r.get('control_direction') else 'OPPOSITE') if active else ('ZERO' if complete else 'UNKNOWN')
    risk = ('BOTH' if candidate and control else 'CANDIDATE_ONLY' if candidate else 'CONTROL_ONLY' if control else 'NEITHER')
    if r.get('risk_valid') is None or r.get('control_risk_valid') is None:
        risk = 'UNKNOWN'
    return dict(trading_date=r['day'], symbol=r['symbol'], csymbol='DCE.M', raw_opportunities=1,
        complete_past_features=int(complete), raw_nonzero_direction_events=int(active),
        feature_unknown=int(not complete), zero_direction=int(complete and not active),
        window_status=window, window_compatible=int(window == 'VALID'),
        implementable_direction_events=int(implementable), nonoverlap_events=int(implementable),
        risk_valid_events=int(implementable and candidate),
        common_candidate_control_risk_events=int(implementable and candidate and control),
        informative_opposite_direction_events=int(implementable and relation == 'OPPOSITE'),
        informative_opposite_common_risk_events=int(implementable and candidate and control and relation == 'OPPOSITE'),
        direction_relation=relation, risk_qualification=risk,
        candidate_direction=r.get('direction'), control_direction=r.get('control_direction'),
        candidate_risk_ticks=r.get('candidate_risk_ticks'), control_risk_ticks=r.get('control_risk_ticks'),
        missing_reasons='|'.join(r.get('issues', []) + ([] if window == 'VALID' else [window])),
        arrival_clock='UNKNOWN', independent_sample=False,
        available_tradable_minutes=r.get('available_tradable_minutes'), source_scope='OLD_ENGINEERING_CAPACITY_ONLY')


def crosscheck(observations):
    """Aggregates only; never accept response/return/price-path fields."""
    from .common import result_columns
    for r in observations:
        if result_columns(r):
            raise ValueError('outcome fields forbidden in capacity crosscheck')
    c = pd.DataFrame([capacity_row(r) for r in observations])
    if c.empty:
        raise ValueError('empty calendar slots')
    rows = []
    def add(section, scope, category, count):
        rows.append(dict(section=section, scope=scope, category=category, count=int(count)))
    for col in ('raw_opportunities', 'complete_past_features', 'feature_unknown', 'zero_direction',
                'raw_nonzero_direction_events', 'implementable_direction_events', 'risk_valid_events',
                'common_candidate_control_risk_events', 'informative_opposite_direction_events', 'informative_opposite_common_risk_events'):
        add('DENOMINATOR', 'ALL_CALENDAR_SLOTS', col, c[col].sum())
    for status in ('VALID', 'NO_ENTRY_WINDOW', 'WINDOW_MISMATCH', 'UNKNOWN'):
        add('WINDOW', 'ALL_CALENDAR_SLOTS', status, (c.window_status == status).sum())
    valid = c.loc[c.implementable_direction_events == 1]
    for relation in ('SAME', 'OPPOSITE'):
        for risk in ('BOTH', 'CANDIDATE_ONLY', 'CONTROL_ONLY', 'NEITHER', 'UNKNOWN'):
            add('DIRECTION_X_RISK', relation, risk,
                ((valid.direction_relation == relation) & (valid.risk_qualification == risk)).sum())
    scopes = {'FEATURE_COMPLETE': c.loc[c.complete_past_features == 1], 'IMPLEMENTABLE_NONZERO': valid,
              'COMMON_RISK': valid.loc[valid.risk_qualification == 'BOTH'],
              'OPPOSITE_COMMON_RISK': valid.loc[(valid.risk_qualification == 'BOTH') & (valid.direction_relation == 'OPPOSITE')]}
    for name, subset in scopes.items():
        for direction in (-1, 1):
            add('CONTROL_DIRECTION', name, 'LONG' if direction == 1 else 'SHORT', (subset.control_direction == direction).sum())
    distributions = []
    for scope, subset in scopes.items():
        for side in ('candidate', 'control'):
            s = pd.to_numeric(subset[side + '_risk_ticks'], errors='coerce')
            finite = s.loc[s.notna() & s.map(lambda x: math.isfinite(x))]
            distributions.append(dict(scope=scope, side=side, known=len(finite), missing=len(s)-len(finite),
                invalid_nonpositive=int((finite <= 0).sum()),
                **{k: float(finite.quantile(q)) if len(finite) else None for k,q in [('min',0),('p25',.25),('median',.5),('p75',.75),('max',1)]}))
    return c, pd.DataFrame(rows), pd.DataFrame(distributions)
