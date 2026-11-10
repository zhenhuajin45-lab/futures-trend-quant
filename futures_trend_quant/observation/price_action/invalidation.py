def hypothesis(path, direction, entry_proof, structure_fact, breakout=None):
    names = dict(A='DIRECTION_TRANSITION', B='ORDERED_PULLBACK_END', C='BOX_CONTINUATION', D='STRONG_BACKGROUND_TRIGGER')
    protected = structure_fact.get('protected_pivot')
    return dict(path=path, hypothesis=names.get(path,'UNKNOWN'), direction=direction,
                original_entry_proof=entry_proof, protected_pivot=protected,
                breakout_boundary=None if not breakout else breakout.get('zone_id'),
                touch_fact='NOT_EVALUATED', closed_damage=structure_fact.get('damage_event'),
                future_structure_confirmation='NOT_EVALUATED', mode='SHADOW', actual_exit_changed=False)


def observe(original,bar,known_at,prior=None):
    """Track the original candidate's anchor, never an actual position exit."""
    import math
    previous=dict(prior or {})
    if previous.get('last_bar_id')==bar.identity:return previous
    value=original.get('anchor');direction=original.get('direction')
    result=dict(candidate_evidence_id=original['candidate_evidence_id'],mode='SHADOW',
                actual_exit_changed=False,actual_position_assumed=False,last_bar_id=bar.identity,
                occurred_at=bar.eob.isoformat(),known_at=known_at,path=original.get('path'))
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0 or direction not in ('bull','bear'):
        return dict(result,status='UNKNOWN',reason='ORIGINAL_HYPOTHESIS_REFERENCE_UNAVAILABLE')
    touched=bar.low<=value<=bar.high
    crossed=bar.low<value if direction=='bull' else bar.high>value
    broken=bar.close<value if direction=='bull' else bar.close>value
    first=previous.get('first_closed_break_at') or (known_at if broken else None)
    return dict(result,status='CLOSED_INVALIDATED' if first else 'WICK_CROSSED' if crossed else 'TOUCHED' if touched else 'NO_CONTACT',
                reference_kind='ORIGINAL_ENTRY_ANCHOR',reference_price=value,reference_id=original.get('anchor_bar_id'),
                touched=touched,wick_crossed=crossed,closed_broken=broken,first_closed_break_at=first,
                exact_c_box_boundary='UNKNOWN' if original.get('path')=='C' else 'NOT_APPLICABLE',
                original_expires_at=original.get('original_expires_at'))
