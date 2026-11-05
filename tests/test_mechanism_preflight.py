"""Past-only data boundaries and capacity semantics, not predictive performance."""
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from tools.mechanism_preflight.common import read,ROOT,authorize_price_span,mde_budget,result_columns
from tools.mechanism_preflight.features import session_observation,trading_minutes,classify_sequence,read_prefixes,require_obi5_fields


def frame(day='2026-07-01',symbol='DCE.m2609'):
    morning=pd.date_range(day+'T09:01:00+08:00',periods=30,freq='min')
    times=list(morning)+[pd.Timestamp(day+'T14:30:00+08:00')]
    return pd.DataFrame(dict(symbol=[symbol]*31,eob=times,open=[100.]*31,high=[102.]*31,low=[99.]*31,close=[101.]*31,volume=[1.]*31))


def clock(day='2026-07-01'):
    return [*pd.date_range(day+'T09:01:00+08:00',day+'T10:15:00+08:00',freq='min'),
        *pd.date_range(day+'T10:31:00+08:00',day+'T11:30:00+08:00',freq='min'),
        *pd.date_range(day+'T13:31:00+08:00',day+'T15:00:00+08:00',freq='min')]


def test_future_price_and_extra_future_rows_cannot_change_past_trigger():
    a=frame();b=pd.concat([a,pd.DataFrame([dict(symbol='DCE.m2609',eob=pd.Timestamp('2026-07-01T14:31:00+08:00'),open=1e9,high=2e9,low=0,close=1e9,volume=1e8)])])
    assert session_observation(a,clock(),'DCE.m2609','2026-07-01')==session_observation(b,clock(),'DCE.m2609','2026-07-01')


def test_midday_and_midnight_and_holiday_have_no_phantom_minutes():
    assert trading_minutes(clock(),'2026-07-01T11:20:00+08:00','2026-07-01T13:40:00+08:00')==20
    ends=[*pd.date_range('2026-09-30T23:51:00+08:00','2026-10-01T00:10:00+08:00',freq='min'),*pd.date_range('2026-10-09T09:01:00+08:00',periods=10,freq='min')]
    assert trading_minutes(ends,'2026-09-30T23:50:00+08:00','2026-10-09T09:10:00+08:00')==30


def test_roll_does_not_borrow_other_contract_prices():
    f=frame();other=frame(symbol='DCE.m2701');f=f.iloc[1:]
    r=session_observation(pd.concat([f,other]),clock(),'DCE.m2609','2026-07-01')
    assert not r['feature_complete'] and 'MISSING_FEATURE_MINUTES' in r['issues']


def test_missing_price_never_filled_zero():
    f=frame();f.loc[0,'close']=np.nan
    r=session_observation(f,clock(),'DCE.m2609','2026-07-01')
    assert not r['feature_complete'] and r['risk_valid'] is None


def test_duplicate_late_and_out_of_order_are_separately_detected():
    e=['2026-07-01T09:30:00+08:00','2026-07-01T09:30:00+08:00','2026-07-01T09:29:00+08:00','2026-07-01T10:00:00+08:00']
    r=[e[0],e[1],e[2],'2026-07-01T10:00:01+08:00'];ends=['2026-07-01T09:55:00+08:00']*3+['2026-07-01T10:25:00+08:00']
    x=classify_sequence(e,r,e,ends)
    assert x[0]['accepted'] and x[1]['duplicate'] and x[2]['out_of_order'] and x[3]['late']
    assert sum(z['accepted'] for z in x)==1


def test_unknown_reception_clock_cannot_be_treated_as_on_time():
    e=['2026-07-01T09:30:00+08:00']
    r=classify_sequence(e,[None],e,['2026-07-01T09:55:00+08:00'])
    assert r[0]['unknown_clock'] and not r[0]['accepted']


def test_m1_cannot_satisfy_obi5_gate():
    with pytest.raises(ValueError,match='OBI5 not testable'):require_obi5_fields(frame().columns)


def test_result_columns_rejected_before_feature_calculation():
    f=frame();f['future_return']=0.
    with pytest.raises(ValueError,match='outcome/result'):session_observation(f,clock(),'DCE.m2609','2026-07-01')


def test_protected_price_is_rejected_before_any_loader(monkeypatch):
    from tools.mechanism_preflight import features
    def forbidden(*a,**k):raise AssertionError('protected body loader called')
    monkeypatch.setattr(features.ds,'dataset',forbidden)
    policy=read(ROOT/'tools/mechanism_preflight/protected.json')
    item=dict(engineering_price_access=False,min_event_at='2025-10-01T09:00:00+08:00',max_event_at='2025-10-02T15:00:00+08:00',purpose='OLD_ENGINEERING_FEATURES_ONLY')
    with pytest.raises(ValueError,match='protected price body'):read_prefixes(item,policy,['2025-10-01'])


def test_no_automatic_independent_access_from_a_date_gap():
    p=read(ROOT/'tools/mechanism_preflight/protected.json')
    assert not authorize_price_span(p,'2026-11-03T09:00:00+08:00','2026-11-03T15:00:00+08:00','OLD_ENGINEERING_FEATURES_ONLY')
    assert not authorize_price_span(p,'2026-11-04T21:00:00+08:00','2026-11-05T15:00:00+08:00','OLD_ENGINEERING_FEATURES_ONLY')


def test_paper30_minute_window_and_operational_adaptation_distinguished():
    r=session_observation(frame(),clock(),'DCE.m2609','2026-07-01')
    assert r['next_legal_model_entry_at']=='2026-07-01T14:31:00+08:00'
    assert r['available_tradable_minutes']==24 and r['fill_model_at']=='NOT_COMPUTED'
    assert r['clock_basis']=='EVENT_TIME_PROXY'


def test_same_data_features_and_control_direction_repeat_exactly():
    f=frame();assert session_observation(f,clock(),'DCE.m2609','2026-07-01')==session_observation(f,clock(),'DCE.m2609','2026-07-01')


def test_budget_is_sensitivity_not_power_estimate():
    a=mde_budget(100);r=next(x for x in a if x['sigma_delta_r']==1 and x['delta_min_r']==.1)
    assert r['required_independent_pairs']==785 and 'NOT_MEASURED_POWER' in r['interpretation']


def test_sparse_pickle_requires_known_original_hash_before_loading(monkeypatch,tmp_path):
    from tools.mechanism_preflight.inventory import trusted_sparse_quote_schema
    def forbidden(*a,**k):raise AssertionError('untrusted pickle loaded')
    monkeypatch.setattr(pd,'read_pickle',forbidden)
    with pytest.raises(ValueError,match='before deserialization'):trusted_sparse_quote_schema(tmp_path/'unknown.pkl',{},read(ROOT/'tools/mechanism_preflight/protected.json'))


def test_parquet_projection_excludes_future_rows_and_rejects_labels(tmp_path):
    from tools.mechanism_preflight.inventory import parquet_metadata
    from tools.mechanism_preflight.common import sha
    p=tmp_path/'raw.parquet';a=frame()
    later=a.iloc[[0]].copy();later.eob=pd.Timestamp('2026-07-01T14:31:00+08:00');later.close=999999
    pd.concat([a,later]).to_parquet(p,index=False)
    m=parquet_metadata(p);item=dict(path=str(p),sha256=sha(p),engineering_price_access=True,purpose='OLD_ENGINEERING_FEATURES_ONLY',**m)
    policy=read(ROOT/'tools/mechanism_preflight/protected.json')
    got=read_prefixes(item,policy,['2026-07-01']);assert len(got)==31 and got.close.max()==101
    a['future_return']=1.;a.to_parquet(p,index=False);item.update(sha256=sha(p))
    with pytest.raises(ValueError,match='result columns forbidden'):read_prefixes(item,policy,['2026-07-01'])


@pytest.mark.parametrize('patch,status',[
    ({'mechanism_complete':False},'STOP_NO_MECHANISM'),
    ({'new_development_window':False},'NEED_DATA_DECISION'),
    ({'capacity_proven_insufficient':True},'NOT_TESTABLE_WITHIN_SCOPE'),
    ({},'READY_FOR_DEVELOPMENT_DESIGN')])
def test_all_four_routes_never_authorize_backtest_or_orders(patch,status):
    from tools.mechanism_preflight.routing import decide
    evidence=dict(mechanism_complete=True,primary_mechanism_evidence=True,control_defined=True,required_feature_fields=True,
        new_development_window=True,separate_independent_window=True,delta_min_registered=True,capacity_design_complete=True,
        level='GROSS_DESIGN',fees_certified=False,gross_protocol_frozen=True,independent_reservation_plan=True)
    evidence.update(patch);r=decide(evidence)
    assert r['status']==status and not r['economic_pass'] and not r['backtest_permission'] and not r['orders_permission']


def test_existing_output_rejected_before_any_source_or_price_access(tmp_path):
    from tools.mechanism_preflight.check import run as check
    from tools.mechanism_preflight.inventory import run as inventory
    out=tmp_path/'already';out.mkdir()
    with pytest.raises(ValueError,match='fresh output'):check(tmp_path/'no_inventory',tmp_path/'no_mechanism',out)
    with pytest.raises(ValueError,match='fresh output'):inventory(tmp_path/'no_index',tmp_path/'no_policy',out)


def test_naive_reception_is_unknown_not_assumed_utc():
    f=frame();f['received_at']='2026-07-01T01:00:00'
    r=session_observation(f,clock(),'DCE.m2609','2026-07-01')
    assert not r['feature_complete'] and 'UNKNOWN_OR_LATE_INPUT_AT_SIGNAL' in r['issues']
    r=classify_sequence(['2026-07-01T09:30:00'],['2026-07-01T09:30:00'],['2026-07-01T09:30:00'],['2026-07-01T09:55:00'])
    assert r[0]['unknown_clock'] and not r[0]['accepted']
