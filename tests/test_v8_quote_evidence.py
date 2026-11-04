from tools.v8_roll_review.quote_evidence import assess_quote


def test_long_uses_opposite_ask_and_actual_lots():
    r=assess_quote('bull',3,100,1,dict(ask_p=102,ask_v=3,bid_p=101,bid_v=20))
    assert r['visible_quantity_sufficient'] and r['within_2_ticks'] and not r['within_1_tick']


def test_short_uses_opposite_bid_and_actual_lots():
    r=assess_quote('bear',4,100,1,dict(ask_p=100,ask_v=20,bid_p=99,bid_v=3))
    assert not r['visible_quantity_sufficient'] and r['status']=='INSUFFICIENT_TOP_DEPTH'
    assert r['adverse_ticks_to_raw_open']==1


def test_missing_opposite_quote_is_not_executable_evidence():
    assert assess_quote('bull',1,100,1,dict(ask_p=0,ask_v=0,bid_p=99,bid_v=3))['status']=='NO_VALID_OPPOSITE_QUOTE'
