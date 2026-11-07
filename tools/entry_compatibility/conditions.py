"""Exact original path predicates, with missing conditions instead of NO_PATH."""
from dataclasses import asdict
from futures_trend_quant.core.entry.paths import entry_h1_reasons


def path_conditions(context, evidence, settings):
    b_h1, d_h1 = entry_h1_reasons(context.h1_state, context.direction, settings.path_b_h1_exception)
    recent = [e for e in (evidence.m5_divergence, evidence.m5_ema_cross, evidence.m5_engulfing)
              if e is not None and (evidence.m5_last_sequence is None or evidence.m5_last_sequence-e.sequence <= 5)]
    common = {'TREND_CONFIRMED': context.trend_confirmed}
    return {
        'A': dict(ENABLED=settings.path_a_enabled, **common,
                  TWO_OF_THREE=sum(e is not None for e in (evidence.m5_divergence,evidence.m5_flatten,evidence.m5_pattern))>=2),
        'B': dict(ENABLED=settings.path_b_enabled, **common, H1_B=b_h1=='PASS',
                  NO_M5_REVERSE=not evidence.m5_reverse_divergence,
                  PULLBACK=evidence.m5_pullback is not None, PATTERN=evidence.m5_pattern is not None),
        'C': dict(ENABLED=settings.path_c_enabled, **common, NO_SAME_POSITION=not context.same_direction_positions,
                  CONSOLIDATION=evidence.m5_consolidation is not None,
                  CONFIRMATION=evidence.m5_breakout is not None or evidence.m5_pattern is not None),
        'D': dict(ENABLED=settings.path_d_enabled, H1_D=d_h1=='PASS',
                  SLOPE=context.m15_slope_state==('up' if context.direction=='bull' else 'down'),
                  NO_M15_REVERSE=not context.m15_reverse_divergence, GATE=context.gate_open,
                  NO_M1_STRONG_REVERSE=not evidence.m1_strong_reverse, RECENT_EVENT=bool(recent)),
    }


def relation(first, second):
    return dict(first=None if first is None else asdict(first), second=None if second is None else asdict(second),
                ordering='MISSING' if first is None or second is None else
                'FIRST_BEFORE_CONFIRM' if first.sequence<second.sequence else
                'SAME_BAR' if first.sequence==second.sequence else 'CONFIRM_BEFORE_FIRST',
                same_process='UNKNOWN_NO_PROCESS_ID',
                mechanism_status='UNKNOWN',
                mechanism_qualified=None,
                reason='Original evidence has no retracement/box episode identity; sequence alone is insufficient')
