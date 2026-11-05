"""Pre-outcome regressions for staged gates, windows and fixed-control capacity."""
import json
import pandas as pd
import pytest
from test_mechanism_preflight import frame, clock
from tools.mechanism_preflight.capacity import capacity_row, crosscheck
from tools.mechanism_preflight.features import session_observation
from tools.mechanism_preflight.routing import decide, by_stage


def observation(expected=None):
    return session_observation(frame(), clock() if expected is None else expected, 'DCE.m2609', '2026-07-01')


def evidence():
    return dict(level='GROSS_DESIGN', mechanism_complete=True, primary_mechanism_evidence=True,
        control_defined=True, required_feature_fields=True, new_development_window=True,
        gross_protocol_frozen=True, independent_reservation_plan=True, delta_min_registered=False)


def test_complete_features_without_entry_do_not_count_implementable_capacity():
    r = observation([x for x in clock() if x.hour < 14])
    assert r['feature_complete'] and r['risk_valid'] and r['next_legal_model_entry_at'] is None
    c = capacity_row(r)
    assert c['raw_opportunities'] == c['raw_nonzero_direction_events'] == 1
    assert c['window_status'] == 'NO_ENTRY_WINDOW'
    assert c['nonoverlap_events'] == c['risk_valid_events'] == c['common_candidate_control_risk_events'] == 0


def test_existing_entry_but_short_window_is_separate_mismatch():
    c = capacity_row(observation([x for x in clock() if x.strftime('%H:%M') != '14:40']))
    assert c['window_status'] == 'WINDOW_MISMATCH' and c['nonoverlap_events'] == 0


def test_duplicate_minute_cannot_mask_a_missing_minute():
    expected = [x for x in clock() if x.strftime('%H:%M') != '14:40']
    expected.append(pd.Timestamp('2026-07-01T14:41:00+08:00'))
    r = observation(expected)
    assert r['available_tradable_minutes'] == 24 and not r['adapted_window_compatible']


def test_old_seed_not_redrawn_for_same_direction_or_invalid_risk():
    r = observation()
    altered = frame(); altered.loc[30, 'close'] = 104
    a = session_observation(altered, clock(), 'DCE.m2609', '2026-07-01')
    assert a['control_direction'] == r['control_direction']
    assert a['direction'] == r['direction'] and a['candidate_risk_ticks'] == 6


def test_risk_four_cells_sum_to_parent_and_same_direction_retained():
    rows = []
    for relation in ('SAME', 'OPPOSITE'):
        for candidate, control in ((True,True),(True,False),(False,True),(False,False)):
            r = dict(observation(), direction=1, control_direction=1 if relation == 'SAME' else -1,
                     risk_valid=candidate, control_risk_valid=control)
            rows.append(r)
    c, table, _ = crosscheck(rows)
    grid = table.loc[table.section == 'DIRECTION_X_RISK']
    assert grid['count'].sum() == c.implementable_direction_events.sum() == 8
    assert c.common_candidate_control_risk_events.sum() == 2
    assert c.informative_opposite_common_risk_events.sum() == 1


def test_raw_zero_unknown_and_no_window_denominators_not_dropped():
    rows = [observation(), dict(observation(), direction=0, nonzero_direction=False),
            dict(day='2026-07-02',symbol='UNKNOWN',feature_complete=False,issues=['MAPPING_UNAVAILABLE']),
            observation([x for x in clock() if x.hour < 14])]
    c, _, distribution = crosscheck(rows)
    assert len(c) == c.raw_opportunities.sum() == 4
    assert c.raw_nonzero_direction_events.sum() == 2 and c.nonoverlap_events.sum() == 1
    assert c.window_status.tolist() == ['VALID','VALID','UNKNOWN','NO_ENTRY_WINDOW']
    assert distribution.known.sum() > 0


def test_gross_design_does_not_require_net_cost_independent_data_or_delta():
    e = evidence(); gates = by_stage(e)
    assert gates['GROSS_DESIGN']['status'] == 'READY_FOR_DEVELOPMENT_DESIGN'
    assert gates['NET_EXECUTABLE']['status'] == gates['INDEPENDENT_VALIDATION']['status'] == 'NEED_DATA_DECISION'
    assert 'HISTORICAL_FEES_NOT_CERTIFIED' in gates['NET_EXECUTABLE']['reasons']
    assert 'NO_QUALIFIED_SEPARATE_INDEPENDENT_WINDOW' in gates['INDEPENDENT_VALIDATION']['reasons']
    for g in gates.values():
        assert not any(g[k] for k in ('economic_pass','backtest_permission','response_run_permission','orders_permission'))
    assert e['delta_min_registered'] is False


@pytest.mark.parametrize('key', ['new_development_window','gross_protocol_frozen','independent_reservation_plan'])
def test_gross_stage_still_requires_new_data_protocol_and_holdout_plan(key):
    e = evidence(); e[key] = None
    assert decide(e)['status'] == 'NEED_DATA_DECISION'


def test_unknown_stage_is_rejected_before_prices_are_opened(monkeypatch,tmp_path):
    from tools.mechanism_preflight import check
    from tools.mechanism_preflight.common import write, sha, ROOT
    inv = tmp_path/'inv'; inv.mkdir()
    csv = inv/'inventory.csv'; csv.write_text('source_id,path\n',encoding='utf8')
    index=tmp_path/'index.json';write(index,{})
    policy=tmp_path/'policy.json';write(policy,{})
    write(inv/'index_binding.json',dict(inventory_sha256=sha(csv),source_index=str(index),protected=str(policy),
        source_index_sha256=sha(index),protected_sha256=sha(policy)))
    m=json.loads((ROOT/'tools/mechanism_preflight/mechanism.yaml').read_text(encoding='utf8'))
    m['research_level']='BAD_STAGE'; config=tmp_path/'m.json';write(config,m)
    def forbidden(*a,**k):raise AssertionError('price loader reached')
    monkeypatch.setattr(check,'read_prefixes',forbidden)
    with pytest.raises(ValueError,match='unknown research level'):check.run(inv,config,tmp_path/'out')


def test_result_fields_rejected_by_metadata_crosscheck():
    with pytest.raises(ValueError,match='outcome fields'):crosscheck([dict(observation(),future_return=1)])


@pytest.mark.parametrize('legal_window', [False, True])
def test_actual_entry_preserves_day_denominator_and_binds_config_level(tmp_path, legal_window):
    from tools.mechanism_preflight.check import run
    from tools.mechanism_preflight.common import write, read, sha, ROOT
    f=frame(); raw=tmp_path/'raw.parquet';f.to_parquet(raw,index=False)
    expected=clock() if legal_window else [x for x in clock() if x.hour<14]
    calendar=tmp_path/'calendar.json'; mapping=tmp_path/'mapping.json'
    write(calendar,[dict(eob=x.isoformat(),trading_date='2026-07-01') for x in expected])
    write(mapping,{'2026-07-01':'DCE.m2609'})
    index=tmp_path/'index.json'
    write(index,dict(calendar_source_id='calendar',mapping_source_id='mapping',feature_source_ids=['bars'],
        sources=[dict(source_id=k,path=str(p)) for k,p in [('calendar',calendar),('mapping',mapping),('bars',raw)]]))
    inv=tmp_path/'inventory';inv.mkdir()
    pd.DataFrame([dict(source_id=k,path=str(p),sha256=sha(p),engineering_price_access=True,
        purpose='OLD_ENGINEERING_FEATURES_ONLY',min_event_at=f.eob.min().isoformat(),max_event_at=f.eob.max().isoformat())
        for k,p in [('calendar',calendar),('mapping',mapping),('bars',raw)]]).to_csv(inv/'inventory.csv',index=False)
    policy=ROOT/'tools/mechanism_preflight/protected.json'
    write(inv/'index_binding.json',dict(inventory_sha256=sha(inv/'inventory.csv'),source_index=str(index),protected=str(policy),
        source_index_sha256=sha(index),protected_sha256=sha(policy)))
    m=read(ROOT/'tools/mechanism_preflight/mechanism.yaml');assert m['delta_min_r'] is None
    config=tmp_path/'config.json';write(config,m)
    out=tmp_path/'result'; result=run(inv,config,out)
    assert result['raw_opportunities']==result['feature_complete_days']==result['nonzero_events']==1
    assert result['implementable_events']==int(legal_window)
    c=pd.read_csv(out/'capacity.csv'); assert c.raw_opportunities.sum()==1
    gates=read(out/'gates_by_stage.json'); decision=read(out/'decision.json')
    assert gates['configured_level']==decision['routing']['research_level']=='GROSS_DESIGN'
    assert gates['mechanism_sha256']==sha(config)
    assert not decision['development_backtest_authorized']
    assert not read(config)['delta_min_r']


def test_source_audit_existing_output_rejected_before_network(tmp_path,monkeypatch):
    from tools.mechanism_preflight import source_audit
    def forbidden(*a,**k):raise AssertionError('network reached')
    monkeypatch.setattr(source_audit,'gm_metadata',forbidden)
    out=tmp_path/'already';out.mkdir()
    with pytest.raises(ValueError,match='fresh output'):source_audit.run(tmp_path/'absent',out,True)


def test_gm_metadata_only_calls_calendar_and_mapping(monkeypatch):
    import sys, types
    from tools.mechanism_preflight import source_audit
    calls=[]
    def record(name,result):
        def f(*a,**k):calls.append(name);return result
        return f
    fake=types.ModuleType('gm.api')
    fake.set_token=record('set_token',None)
    fake.get_trading_dates=record('get_trading_dates',['2024-01-02'])
    fake.fut_get_continuous_contracts=record('fut_get_continuous_contracts',[dict(symbol='DCE.m2405',trade_date='2024-01-02')])
    monkeypatch.setitem(sys.modules,'gm.api',fake)
    monkeypatch.setenv('GM_TOKEN','SYNTHETIC_PLACEHOLDER')
    monkeypatch.setattr(source_audit.importlib.util,'find_spec',lambda x:True)
    result, original=source_audit.gm_metadata()
    assert calls==['set_token','get_trading_dates','fut_get_continuous_contracts']
    assert result['price_requests']==0 and result['calendar_days']==1
    assert original['mapping'][0]['symbol']=='DCE.m2405'
