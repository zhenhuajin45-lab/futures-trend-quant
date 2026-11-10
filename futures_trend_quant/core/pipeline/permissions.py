"""One preregistered paper permission version; no change to background voting."""
from dataclasses import dataclass

POLICY = 'CLOSED_FACTS_PERMISSION_V2_20261111'


@dataclass(frozen=True)
class TradePermission:
    policy: str
    background: str
    new_entry_allowed: bool
    reasons: tuple[str, ...]
    technical_exit_direction: str | None
    reverse_authorized: str | None
    weak_closed_bars: int
    source_eob: str


def evaluate_permission(background, *, close_side, slope, hist, opposite_divergence,
                        closed_h1_known, calendar_qualified, previous_weak, changed, eob):
    reasons = []
    if background not in ('bull', 'bear'):
        return TradePermission(POLICY, background, False, ('NO_DIRECTION_AUTHORIZATION',),
                               None, None, 0, eob)
    long = background == 'bull'
    aligned_close = close_side == ('above' if long else 'below')
    aligned_slope = slope == ('up' if long else 'down')
    aligned_macd = hist is not None and (hist > 0 if long else hist < 0)
    if not aligned_close: reasons.append('M15_CLOSE_ADVERSE')
    if not aligned_slope: reasons.append('M15_SLOPE_NOT_ALIGNED')
    if not aligned_macd: reasons.append('M15_MACD_NOT_ALIGNED')
    if opposite_divergence: reasons.append('M15_OPPOSITE_DIVERGENCE_ACTIVE')
    if not closed_h1_known: reasons.append('CLOSED_H1_UNKNOWN')
    if not calendar_qualified: reasons.append('H1_CALENDAR_UNQUALIFIED')
    adverse_macd = hist is not None and (hist < 0 if long else hist > 0)
    weak = previous_weak + 1 if background in ('bull', 'bear') and not aligned_close and adverse_macd else 0
    return TradePermission(POLICY, background, not reasons, tuple(reasons),
                           background if weak >= 2 else None,
                           background if changed and background in ('bull', 'bear') else None,
                           weak, eob)
