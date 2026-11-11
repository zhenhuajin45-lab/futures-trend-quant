"""Expected exchange-close watermark is diagnostic, never a synthetic Bar."""
from datetime import datetime
import pandas as pd

POLICY='CLOSED_PREFIX_WATERMARK_V1_20261111'


def closure_status(calendar,progress,now,source):
    checked=pd.Timestamp(now)
    if checked.tzinfo is None:raise ValueError('watermark timezone unknown')
    index=int(calendar.ns.searchsorted(checked.value,side='right'))-1
    expected_m1=None if index<0 else calendar.clock[index]
    m15_index=index
    while m15_index>=0 and int(calendar.ns[m15_index])%900_000_000_000:m15_index-=1
    expected_m15=None if m15_index<0 else calendar.clock[m15_index]
    def age(key,expected):
        raw=progress.get(key)
        if raw is None or expected is None:return dict(actual=raw,expected=None if expected is None else expected.isoformat(),status='UNKNOWN')
        actual=pd.Timestamp(raw)
        if actual.tzinfo is None:return dict(actual=raw,expected=expected.isoformat(),status='UNKNOWN')
        status='FUTURE_WATERMARK' if actual>checked else 'CURRENT_CLOSED_PREFIX' if actual>=expected else 'PENDING_SOURCE_CLOSURE'
        return dict(actual=actual.isoformat(),expected=expected.isoformat(),status=status,
            expected_minutes_after_watermark=max(0,int(calendar.ns.searchsorted(expected.value,side='right')-calendar.ns.searchsorted(actual.value,side='right'))))
    return dict(policy=POLICY,checked_at=checked.isoformat(),source=source,
        m1=age('last_closed_m1',expected_m1),m15=age('last_closed_m15',expected_m15),
        forming_promoted=False,affects_execution=False,
        note='COMPLETE existing windows do not prove the latest expected source tail has arrived')
