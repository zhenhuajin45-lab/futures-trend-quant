"""Resource routing only; ready-for-design never authorizes backtests or orders."""


def decide(evidence):
    reasons=[]
    if not evidence.get('mechanism_complete') or not evidence.get('primary_mechanism_evidence') or not evidence.get('control_defined'):
        status='STOP_NO_MECHANISM';reasons.append('TRIGGER_WINDOW_EXIT_ANCHOR_CONTROL_OR_PRIMARY_EVIDENCE_INCOMPLETE')
    elif evidence.get('scope_incompatible') or evidence.get('capacity_proven_insufficient'):
        status='NOT_TESTABLE_WITHIN_SCOPE';reasons.append('AUTHORIZED_FIXED_SCOPE_CANNOT_SUPPORT_WINDOW_ANCHOR_OR_CAPACITY')
    else:
        if not evidence.get('required_feature_fields'):reasons.append('REQUIRED_FEATURE_FIELDS_NOT_QUALIFIED')
        if not evidence.get('new_development_window'):reasons.append('NO_QUALIFIED_NONOVERLAPPING_NEW_DEVELOPMENT_WINDOW')
        if not evidence.get('separate_independent_window'):reasons.append('NO_QUALIFIED_SEPARATE_INDEPENDENT_WINDOW')
        if not evidence.get('delta_min_registered'):reasons.append('DELTA_MIN_NOT_REGISTERED_OWNER_REQUESTS_SENSITIVITY_ONLY')
        if not evidence.get('capacity_design_complete'):reasons.append('CAPACITY_IS_BOUND_ONLY_NOT_EFFECTIVE_PAIR_OR_POWER_QUALIFICATION')
        if evidence.get('level')=='NET_EXECUTABLE':
            if not evidence.get('entry_exit_quotes'):reasons.append('ENTRY_AND_EXIT_QUOTE_COVERAGE_ABSENT_AT_CANDIDATE_TIMES')
            if not evidence.get('arrival_and_delay'):reasons.append('SYNCHRONIZED_ARRIVAL_AND_BROKER_DELAY_UNVERIFIED')
            if not evidence.get('fees_certified'):reasons.append('HISTORICAL_FEES_NOT_CERTIFIED')
        elif evidence.get('level')!='GROSS_DESIGN':reasons.append('RESEARCH_LEVEL_NOT_DECLARED')
        status='NEED_DATA_DECISION' if reasons else 'READY_FOR_DEVELOPMENT_DESIGN'
    return dict(status=status,reasons=reasons,economic_pass=False,backtest_permission=False,orders_permission=False,
        research_level=evidence.get('level','UNVERIFIED'),interpretation='FIRST_BATCH_RESOURCE_ROUTING_NOT_STRATEGY_PROMOTION')
