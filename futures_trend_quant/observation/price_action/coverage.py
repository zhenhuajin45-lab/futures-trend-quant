"""Explain exact retained-prefix gaps without inventing historical clocks."""
import hashlib
import json


def describe(minutes, calendar, received):
    if not minutes:
        return dict(status='UNKNOWN', reason='NO_MINUTES', missing_count=None)
    present = {b.eob.isoformat() for b in minutes}
    required = {t.isoformat() for t in calendar.clock if minutes[0].eob <= t <= minutes[-1].eob}
    missing = sorted(required - present)
    unknown = [b.identity for b in minutes if not received.get(b.identity)]
    return dict(policy='PA_INPUT_COVERAGE_DIAGNOSTIC_V1_20261111',
                status='GAP' if missing else 'COMPLETE_CLOSED_PREFIX',
                required_count=len(required), present_required_count=len(required & present),
                missing_count=len(missing), missing_eob=missing,
                required_sha256=hashlib.sha256(json.dumps(sorted(required)).encode()).hexdigest(),
                present_sha256=hashlib.sha256(json.dumps(sorted(present)).encode()).hexdigest(),
                receipt_missing_count=len(unknown), receipt_missing_ids=unknown,
                receipt_scope='CURRENT_RUN_CACHE_OR_BAR_RECEIPT;NOT_HISTORICAL_VENDOR_AVAILABILITY',
                historical_first_received_at='UNKNOWN', retained_from=minutes[0].eob.isoformat(),
                retained_to=minutes[-1].eob.isoformat())


def snapshot_coverage(minutes, calendar, received, m15, m5):
    """Check exactly the analysis horizon; retain broader history diagnostics."""
    inputs=list(m15)+list(m5)
    if not inputs:return describe([],calendar,received)
    begin=min(b.bob for b in inputs);end=max(b.eob for b in inputs)
    retained=[b for b in minutes if begin<b.eob<=end]
    result=describe(retained,calendar,received)
    # Include a missing leading minute rather than narrowing to the first present
    # minute. The actual required interval comes from the consumer, not the data.
    required={t.isoformat() for t in calendar.clock if begin<t<=end}
    present={b.eob.isoformat() for b in retained}
    missing=sorted(required-present)
    result.update(status='GAP' if missing else 'COMPLETE_CLOSED_PREFIX',
                  required_count=len(required),present_required_count=len(required & present),
                  missing_count=len(missing),missing_eob=missing,
                  required_sha256=hashlib.sha256(json.dumps(sorted(required)).encode()).hexdigest(),
                  scope='ACTUAL_M15_M5_CONSUMER_WINDOW',required_from=begin.isoformat(),required_to=end.isoformat(),
                  retained_prefix=describe(minutes,calendar,received))
    return result
