"""Descriptive quality; no pattern alone creates a trading signal."""


def quality(bar, previous=None, atr=None, mother=None, normalized_no_trade=False):
    span = bar.high-bar.low
    body = abs(bar.close-bar.open)
    result = dict(bar_id=bar.identity, body_ratio=None, upper_wick_ratio=None, lower_wick_ratio=None,
                  close_location=None, range_atr=None, patterns=[], related_zone_ids=[],
                  mother_id=None, creates_order=False)
    if normalized_no_trade or bar.volume <= 0:
        return dict(result, status='UNKNOWN', reason='NO_TRADE_OR_NORMALIZED_BAR')
    if span <= 0:
        return dict(result, status='UNKNOWN', reason='ZERO_RANGE')
    upper, lower = bar.high-max(bar.open, bar.close), min(bar.open, bar.close)-bar.low
    result.update(status='DESCRIPTIVE_ONLY', body_ratio=body/span, upper_wick_ratio=upper/span,
                  lower_wick_ratio=lower/span, close_location=(bar.close-bar.low)/span,
                  range_atr=None if not atr or atr <= 0 else span/atr)
    if body == 0:
        result['reason'] = 'ZERO_BODY_NO_AUTOMATIC_PIN'
    from futures_trend_quant.core.facts.bar_arithmetic import calculate_bar_arithmetic,is_bearish_engulfing,is_bullish_engulfing
    arithmetic=calculate_bar_arithmetic(bar,previous,None)
    result['original_pin_measurement']=dict(upper=arithmetic.upper_pinbar,lower=arithmetic.lower_pinbar,
                                            zero_body=body==0,trade_qualification='NOT_EVALUATED')
    # Existing production Pin/engulf rules remain untouched; these are measurements.
    if previous:
        prior=calculate_bar_arithmetic(previous,None,None)
        result['original_engulf_measurement']=dict(bull=is_bullish_engulfing(arithmetic,prior),bear=is_bearish_engulfing(arithmetic,prior))
        if min(bar.open, bar.close) <= min(previous.open, previous.close) and max(bar.open, bar.close) >= max(previous.open, previous.close) and (bar.close-bar.open)*(previous.close-previous.open) < 0:
            result['patterns'].append('BODY_ENGULF_MEASUREMENT')
        parent = mother or previous
        if bar.high <= parent.high and bar.low >= parent.low:
            result['patterns'].append('INSIDE_BAR')
            result['mother_id'] = parent.identity
    return result
