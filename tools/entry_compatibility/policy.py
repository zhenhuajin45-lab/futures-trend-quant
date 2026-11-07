"""Research-only pure policy; no broker, config mutation or production call site."""
from dataclasses import replace
from math import isfinite
from futures_trend_quant.core.pipeline.permissions import evaluate_permission

POLICY = 'B_RETRACE_HIST_ADVISORY_V1'


def evaluate_selected(path, background, *, enabled=False, **facts):
    original = evaluate_permission(background, **facts)
    if not enabled or path != 'B':
        return original
    # Re-evaluate the full fact contract. Never alter hist or technical exits.
    long = background == 'bull'
    checks = [
        (background in ('bull', 'bear'), 'NO_DIRECTION_AUTHORIZATION'),
        (facts['close_side'] == ('above' if long else 'below'), 'M15_CLOSE_ADVERSE'),
        (facts['slope'] == ('up' if long else 'down'), 'M15_SLOPE_NOT_ALIGNED'),
        (not facts['opposite_divergence'], 'M15_OPPOSITE_DIVERGENCE_ACTIVE'),
        (facts['closed_h1_known'], 'CLOSED_H1_UNKNOWN'),
        (facts['calendar_qualified'], 'H1_CALENDAR_UNQUALIFIED'),
        (isinstance(facts['hist'], (int,float)) and isfinite(facts['hist']), 'M15_MACD_UNKNOWN'),
    ]
    reasons = tuple(reason for passed, reason in checks if not passed)
    return replace(original, policy=POLICY, new_entry_allowed=not reasons, reasons=reasons)
