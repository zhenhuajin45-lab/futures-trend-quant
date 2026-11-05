"""Stage-specific design gates. No route grants response/backtest/order access."""
LEVELS = ('GROSS_DESIGN', 'NET_EXECUTABLE', 'INDEPENDENT_VALIDATION')
GROSS = {
    'required_feature_fields': 'REQUIRED_FEATURE_FIELDS_NOT_QUALIFIED',
    'new_development_window': 'NO_QUALIFIED_NONOVERLAPPING_NEW_DEVELOPMENT_WINDOW',
    'gross_protocol_frozen': 'GROSS_RESPONSE_PROTOCOL_NOT_FROZEN',
    'independent_reservation_plan': 'NO_UNCONTAMINATED_INDEPENDENT_RESERVATION_PLAN',
}
NET = {
    'gross_development_passed': 'GROSS_DEVELOPMENT_NOT_ACCEPTED',
    'entry_exit_quotes': 'ENTRY_AND_EXIT_QUOTE_COVERAGE_ABSENT_AT_CANDIDATE_TIMES',
    'arrival_and_delay': 'SYNCHRONIZED_ARRIVAL_AND_BROKER_DELAY_UNVERIFIED',
    'fees_certified': 'HISTORICAL_FEES_NOT_CERTIFIED',
    'delta_min_registered': 'DELTA_MIN_NOT_REGISTERED_OWNER_REQUESTS_SENSITIVITY_ONLY',
    'capacity_design_complete': 'CAPACITY_IS_BOUND_ONLY_NOT_EFFECTIVE_PAIR_OR_POWER_QUALIFICATION',
}
INDEPENDENT = {
    'separate_independent_window': 'NO_QUALIFIED_SEPARATE_INDEPENDENT_WINDOW',
    'candidate_frozen': 'CANDIDATE_NOT_FROZEN',
    'independent_protocol_frozen': 'INDEPENDENT_PROTOCOL_NOT_FROZEN',
    'net_development_passed': 'NET_DEVELOPMENT_NOT_ACCEPTED',
}


def decide(evidence):
    level = evidence.get('level')
    if level not in LEVELS:
        raise ValueError('unknown research level')
    reasons = []
    if any(evidence.get(k) is not True for k in ('mechanism_complete', 'primary_mechanism_evidence', 'control_defined')):
        status = 'STOP_NO_MECHANISM'
        reasons.append('TRIGGER_WINDOW_EXIT_ANCHOR_CONTROL_OR_PRIMARY_EVIDENCE_INCOMPLETE')
    elif evidence.get('scope_incompatible') is True or evidence.get('capacity_proven_insufficient') is True:
        status = 'NOT_TESTABLE_WITHIN_SCOPE'
        reasons.append('AUTHORIZED_FIXED_SCOPE_CANNOT_SUPPORT_WINDOW_ANCHOR_OR_CAPACITY')
    else:
        requirements = {**GROSS, **(NET if level != 'GROSS_DESIGN' else {}),
                        **(INDEPENDENT if level == 'INDEPENDENT_VALIDATION' else {})}
        reasons = [reason for key, reason in requirements.items() if evidence.get(key) is not True]
        status = 'NEED_DATA_DECISION' if reasons else 'READY_FOR_DEVELOPMENT_DESIGN'
    return dict(status=status, reasons=reasons, economic_pass=False, backtest_permission=False,
        response_run_permission=False, orders_permission=False, research_level=level,
        interpretation='STAGE_RESOURCE_ROUTING_ONLY_NOT_RESEARCH_AUTHORIZATION_OR_PROMOTION')


def by_stage(evidence):
    return {level: decide(dict(evidence, level=level)) for level in LEVELS}
