import pandas as pd
import pytest
from tools.first_entry_v71.supplement_v712 import check_jd_candidate,day_minutes


def sample():
    times=sorted(day_minutes('2025-11-04')|day_minutes('2025-11-05'))
    c=pd.DataFrame(dict(instrument='jd2605.DCE',date=times,open=100.,high=101.,low=99.,close=100.,volume=3,open_interest=7))
    r=c[c.date.dt.day==5].rename(columns={'date':'eob','open_interest':'close_oi'})
    return c,r


def test_exact_overlap_permits_only_source_review():
    c,r=sample();out,table=check_jd_candidate(c,r)
    assert out['equal_minutes']==225 and out['may_proceed_to_source_review']
    assert out['strict_qualified_legs']==0 and len(table)==225


def test_one_volume_difference_blocks_merging():
    c,r=sample();c.loc[c.index[-1],'volume']=4
    out,_=check_jd_candidate(c,r)
    assert out['different_minutes']==1 and not out['may_proceed_to_source_review']


def test_missing_overlap_day_cannot_use_missing_day_alone():
    c,r=sample()
    with pytest.raises(ValueError,match='both candidate days'):check_jd_candidate(c[c.date.dt.day==4],r)


def test_wrong_actual_contract_is_rejected():
    c,r=sample();c['instrument']='jd8888.DCE'
    with pytest.raises(ValueError,match='wrong contract'):check_jd_candidate(c,r)


def test_shifted_minute_labels_are_rejected():
    c,r=sample();c['date']-=pd.Timedelta(minutes=1)
    with pytest.raises(ValueError,match='both candidate days'):check_jd_candidate(c,r)
