"""Synthetic full-output decisions and actual entry guards; never read real space results."""
from pathlib import Path
from copy import deepcopy
import json
import numpy as np
import pandas as pd
import pytest
from tools.v11_diagnostic.statistics import analyze,partition,stats
from tools.v11_diagnostic.summarize import run
from tools.v11_diagnostic import contracts as c


def fixture(weights=None):
    weights=weights or [20]*6;rows=[]
    for product,n in enumerate(weights):
        for time in (1,2,3):
            for space in (1,2,3):
                for i in range(n):
                    path='TARGET' if i%5<(1 if space==1 else 3 if space==2 else 4) else 'STOP'
                    r=(space-2)*.5+(i%3-1)*.01
                    rows.append(dict(trade_id=len(rows)+1,csymbol='P'+str(product),symbol='P'+str(product)+'.ACTUAL',
                        entry_day='2026-05-'+str(i%20+1).zfill(2),gross_r=r,remaining_minutes=time*100,
                        hold_minutes=20,range_ratio=float(space),space_status='SYNTHETIC_KNOWN',price_path=path))
    return pd.DataFrame(rows)


def reg(f):return dict(study='SYNTHETIC_V11',cohort=dict(closed=len(f),open=5))


def result(f):return analyze(f,reg(f))


def test_all_conditions_only_release_blind_review_and_all_tables():
    r=result(fixture());assert r['decision']['status']=='READY_FOR_BLIND_REVIEW'
    assert len(r['cells'])==13 and len(r['fixed_2r'])>=9
    assert not r['decision']['automatically_releases_new_sample']
    assert not r['decision']['simulation_orders']


def test_missing_exactly_20pct_and_one_more():
    f=fixture();f.loc[f.index%5==0,'range_ratio']=np.nan
    a=result(f);assert a['coverage']['unknown_ratio']==.2
    assert not any(x['code']=='UNKNOWN_DIMENSIONS_GT_20PCT' for x in a['decision']['reasons']['INDETERMINATE'])
    f.loc[1,'range_ratio']=np.nan;b=result(f)
    assert b['decision']['status']=='INDETERMINATE'
    assert any(x['code']=='UNKNOWN_DIMENSIONS_GT_20PCT' for x in b['decision']['reasons']['INDETERMINATE'])


def test_21pct_missing_beats_apparent_improvement():
    f=fixture();f.loc[:int(len(f)*.21),'range_ratio']=np.nan
    assert result(f)['decision']['status']=='INDETERMINATE'


def endpoint(n):
    f=fixture();high=f.range_ratio==3;f.loc[high,'range_ratio']=2.
    groups=f.loc[high].groupby(['csymbol','remaining_minutes']).groups
    selected=[]
    for i,indices in enumerate(groups.values()):selected+=list(indices)[:n//18+(i<n%18)]
    f.loc[selected,'range_ratio']=3.
    return result(f)


def test_99_vs_100_is_global_end_not_diagonal_cell():
    a=endpoint(99);b=endpoint(100)
    assert any(x['code']=='TERTILE_END_LT_100' for x in a['decision']['reasons']['INDETERMINATE'])
    assert not any(x['code']=='TERTILE_END_LT_100' for x in b['decision']['reasons']['INDETERMINATE'])
    assert a['decision']['status']=='INDETERMINATE'


def test_51pct_single_product_is_concentration_not_data_failure():
    f=fixture([51,10,10,10,10,9]);r=result(f)
    assert r['decision']['status']=='STOP_PRODUCT_MIX'
    assert any(x['code']=='MATCHED_SIDE_PRODUCT_GT_50PCT' for x in r['decision']['reasons']['STOP_PRODUCT_MIX'])


def test_exactly_50pct_not_automatically_concentration():
    r=result(fixture([50,10,10,10,10,10]))
    assert not any(x['code']=='MATCHED_SIDE_PRODUCT_GT_50PCT' for x in r['decision']['reasons']['STOP_PRODUCT_MIX'])


def test_every_tied_largest_product_checked_without_repartitioning():
    f=fixture();effects=[1.,.1,.1,.1,-.2,-.2]
    f.gross_r=[effects[int(p[1:])]*(s-2) for p,s in zip(f.csymbol,f.range_ratio)]
    r=result(f);c=r['decision']['comparisons']['tertiles']
    assert len(c['leave_top_combined_product_out'])==6
    assert r['decision']['status']=='STOP_PRODUCT_MIX'
    assert any(x['code']=='DEPENDENT_ON_TIED_TOP_PRODUCT' for x in r['decision']['reasons']['STOP_PRODUCT_MIX'])


def test_improving_mean_alone_does_not_pass_distribution_requirement():
    f=fixture();f.gross_r=[(5 if s==3 else 4 if s==1 else 4.5) if i%5==4 else -1 for i,s in zip(f.index,f.range_ratio)]
    r=result(f);assert r['decision']['status']=='STOP_MECHANISM'
    assert any(x['code']=='DISTRIBUTION_UNSTABLE' for x in r['decision']['reasons']['STOP_MECHANISM'])


def test_registration_cannot_claim_different_thresholds():
    f=fixture();r=reg(f);r['thresholds']={}
    with pytest.raises(ValueError,match='registered thresholds'):analyze(f,r)


def test_pooled_and_within_product_reverse_sign():
    rows=[]
    for p in range(6):
        for t in (1,2,3):
            for s,n in [(1,10 if p<3 else 100),(2,55),(3,100 if p<3 else 10)]:
                for i in range(n):rows.append(dict(trade_id=len(rows)+1,csymbol='P'+str(p),symbol='TEST',entry_day='d',
                    gross_r=(1 if p<3 else -1)-(s-1)*.01,remaining_minutes=t*100,hold_minutes=10,
                    range_ratio=float(s),space_status='KNOWN',price_path='TARGET' if i%5<3 else 'STOP'))
    r=result(pd.DataFrame(rows));a=r['decision']['comparisons']['tertiles']
    assert a['pooled_all_difference']>0 and a['differences']['mean_gross_r']<0
    assert r['decision']['status']=='STOP_PRODUCT_MIX'


def test_two_path_directions_conflict_not_pass():
    f=fixture();low=f.range_ratio==1;high=f.range_ratio==3
    f.loc[low,'price_path']='NEITHER';f.loc[low&(f.index%5==0),'price_path']='TARGET';f.loc[low&(f.index%5==1),'price_path']='STOP'
    f.loc[high,'price_path']='STOP';f.loc[high&(f.index%5<3),'price_path']='TARGET'
    r=result(f);assert r['decision']['status']=='STOP_MECHANISM'
    assert any(x['code']=='PATH_CONFLICT' for x in r['decision']['reasons']['STOP_MECHANISM'])


def test_fixed_2r_threshold_no_separation_even_if_quantiles_good():
    f=fixture();f['gross_r']=np.select([f.range_ratio==1,f.range_ratio==2],[0.,-.4],default=.4)
    r=result(f)
    assert r['decision']['comparisons']['tertiles']['differences']['mean_gross_r']>0
    assert abs(r['decision']['comparisons']['fixed_2r']['differences']['mean_gross_r'])<1e-15
    assert r['decision']['status']=='STOP_MECHANISM'


def test_fixed_boundary_equal_two_in_retained_group():
    f=fixture();r=result(f);t=r['fixed_2r']
    assert int(t.loc[t.space_group=='GE_2_ALL','n'].iloc[0])==int((f.range_ratio>=2).sum())


def test_no_comparable_fixed_groups_is_indeterminate():
    f=fixture();f['range_ratio']+=3
    r=result(f);assert r['decision']['status']=='INDETERMINATE'
    assert r['decision']['comparisons']['fixed_2r']['eligible_products']==0


def test_ties_not_split_and_empty_cells_preserved():
    b,cuts=partition(pd.Series([1.,1.,1.,1.,2.,2.]),3)
    assert len(set(b[:4]))==1 and len(set(b[4:]))==1
    f=fixture();f['range_ratio']=2.;r=result(f)
    assert len(r['cells'])==13 and (r['cells'].n==0).any()
    assert r['decision']['status']=='INDETERMINATE'


@pytest.mark.parametrize('path',['AMBIGUOUS','NOT_COMPUTABLE','MISSING'])
def test_unsortable_paths_stay_in_denominator(path):
    f=fixture();f.loc[f.range_ratio==3,'price_path']=path;r=result(f)
    assert r['decision']['status']=='INDETERMINATE'
    high=r['decision']['comparisons']['tertiles']['high']
    assert high['unknown_plus_double']==high['n'] and high['sortable']==0


def test_path_20pct_limit_not_stricter_by_rounding():
    f=fixture();f.loc[(f.range_ratio==3)&(f.index%5==0),'price_path']='AMBIGUOUS'
    r=result(f);issues=r['decision']['reasons']['INDETERMINATE']
    assert not any(x['code']=='UNORDERED_PATH_GT_20PCT' and x['scheme']=='tertiles' for x in issues)
    f.loc[f.index[(f.range_ratio==3)&(f.index%5!=0)][0],'price_path']='MISSING'
    assert any(x['code']=='UNORDERED_PATH_GT_20PCT' and x['scheme']=='tertiles' for x in result(f)['decision']['reasons']['INDETERMINATE'])


def test_trim_contribution_quantiles_and_zero_denominator():
    f=fixture().iloc[:10].copy();f.gross_r=[-100,-2,-1,0,1,2,3,4,5,100]
    a=stats(f);assert a['trimmed_mean_gross_r']==1.5
    assert a['max_absolute_contribution']==100/218
    f.gross_r=0;z=stats(f);assert z['max_absolute_contribution'] is None and z['max_profit_contribution'] is None


def test_synthetic_cli_emits_full_set_and_never_formal_pass(tmp_path):
    f=fixture();folder=tmp_path/'synthetic_input';folder.mkdir();f.to_csv(folder/'audit.csv',index=False)
    c.write(folder/'synthetic.json',dict(data_kind='SYNTHETIC_ONLY'));p=tmp_path/'reg.json';c.write(p,reg(f))
    out=tmp_path/'synthetic_output';d=run(folder,p,None,out,True)
    assert d['data_kind']=='SYNTHETIC_ONLY' and not d['real_economic_verdict']
    assert {p.name for p in out.iterdir()}=={'cells.csv','cell_products.csv','within_product.csv','fixed_2r.csv','path.csv','coverage.json','decision.json','manifest.json'}
    with pytest.raises(ValueError,match='already exists'):run(folder,p,None,out,True)


def frozen_fixture(tmp_path,monkeypatch):
    root=tmp_path/'repo';root.mkdir();monkeypatch.setattr(c,'ROOT',root)
    monkeypatch.setattr(c,'REQUIRED_FILES',{'prereg.json','sentinel.py'})
    p=root/'prereg.json';c.write(p,dict(status='FROZEN_FOR_ONE_OLD_WINDOW_DIAGNOSTIC',review_mode='SINGLE_AGENT_USER_OVERRIDE'))
    code=root/'sentinel.py';code.write_text('original code',encoding='utf8')
    c.write(root/'freeze_manifest.json',dict(hash_convention='SHA256_LF_NORMALIZED_PROJECT_TEXT_RAW_EXTERNAL_BYTES',originals=c.EXPECTED,files={'prereg.json':c.sha(p,True),'sentinel.py':c.sha(code,True)}))
    receipts=tmp_path/'receipts';receipts.mkdir();h=c.sha(root/'freeze_manifest.json',True)
    for name,role in [('execution','EXECUTOR'),('review','REVIEWER')]:c.write(receipts/(name+'.json'),dict(state='SIGNED',role=role,identity='Codex',manifest_sha256=h,recorded_at_beijing='2026-11-04T22:00:00+08:00',independent_person=False,user_authority='USER_ASSIGNED_BOTH_ROLES_TO_CODEX'))
    c.write(receipts/'timestamp_archive.json',dict(manifest_sha256=h,kind='GITHUB_FREEZE_HTTP_DATE',server_date_utc='2026-11-04T14:00:00Z',freeze_commit='test',remote_manifest_blob='test'))
    return p,receipts,code


def test_receipt_no_fake_independent_person_and_missing_receipt(tmp_path,monkeypatch):
    p,r,_=frozen_fixture(tmp_path,monkeypatch);reg,b=c.validate_freeze(p,r)
    assert not b['independent_two_person_review']
    (r/'review.json').unlink()
    with pytest.raises(ValueError,match='AWAITING_SIGNOFF'):c.validate_freeze(p,r)


def test_code_digest_mismatch_refuses_before_reading_audit(tmp_path,monkeypatch):
    p,r,code=frozen_fixture(tmp_path,monkeypatch);code.write_text('changed',encoding='utf8')
    with pytest.raises(ValueError,match='digest mismatch'):c.validate_freeze(p,r)


def test_same_ai_cannot_claim_two_independent_signatures(tmp_path,monkeypatch):
    p,r,_=frozen_fixture(tmp_path,monkeypatch);v=c.read(r/'review.json');v['independent_person']=True;c.write(r/'review.json',v)
    with pytest.raises(ValueError,match='masquerade'):c.validate_freeze(p,r)


def test_timestamp_archive_required(tmp_path,monkeypatch):
    p,r,_=frozen_fixture(tmp_path,monkeypatch);c.write(r/'timestamp_archive.json',{})
    with pytest.raises(ValueError,match='timestamp archive'):c.validate_freeze(p,r)


def test_non_original_queue_refused(tmp_path,monkeypatch):
    monkeypatch.setattr(c,'verify_original_paths',lambda p:None)
    f=fixture();d=tmp_path/'input';d.mkdir();f.to_csv(d/'audit.csv',index=False)
    c.write(d/'summary.json',dict(closed=631,open_excluded=5,known_time=629,clock_hashes_matched=12))
    c.write(d/'provenance.json',dict(data_kind='REAL_OLD_WINDOW',manifest_sha256='x',originals=c.EXPECTED,audit_sha256=c.sha(d/'audit.csv')))
    reg=dict(cohort=dict(ledger_lf_sha256=c.sha(c.ROOT/'reports/v10_same_discipline/GROSS_ORDINARY_SHORT_trades.csv',True),queue_identity_sha256='wrong'))
    with pytest.raises(ValueError,match='not original queue'):c.validate_audit(d,reg,dict(manifest_sha256='x'))


def test_synthetic_input_cannot_enter_formal_input_contract(tmp_path):
    c.write(tmp_path/'provenance.json',dict(data_kind='SYNTHETIC_ONLY'))
    with pytest.raises(ValueError,match='synthetic/draft'):c.validate_audit(tmp_path,{},dict(manifest_sha256='x'))


def test_actual_original_byte_paths_required_not_only_claimed_digest():
    with pytest.raises(ValueError,match='byte paths absent'):c.verify_original_paths({})


def test_source_inventory_cannot_omit_code(tmp_path,monkeypatch):
    p,r,_=frozen_fixture(tmp_path,monkeypatch)
    manifest=p.parent/'freeze_manifest.json';obj=c.read(manifest);obj['files'].pop('sentinel.py');c.write(manifest,obj)
    with pytest.raises(ValueError,match='incomplete frozen'):c.validate_freeze(p,r)


def test_independent_arithmetic_on_synthetic_history_and_future_mutation():
    import importlib.util
    from types import SimpleNamespace
    from tools.v11_diagnostic.workflow import arithmetic
    path=c.ROOT/'tools/v11_diagnostic/reference_tool/test_diagnose.py'
    import sys
    sys.path.insert(0,str(path.parent))
    spec=importlib.util.spec_from_file_location('old_synthetic_cases',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    old=__import__('diagnose');case=m.Fixture();case.setUp()
    row=SimpleNamespace(signal_eob=pd.Timestamp(case.now,tz='UTC').isoformat(),entry_bob=pd.Timestamp(case.now,tz='UTC').isoformat(),
        entry_day=case.clock.dates[case.i],stop=98.,direction='bull')
    a=arithmetic(old,case.leg,case.clock,row)
    assert a['manual_ratio']==a['program_ratio']==2. and len(a['reference_windows'])==20
    case.leg['values'][case.i:,1]=99999
    assert arithmetic(old,case.leg,case.clock,row)['manual_ratio']==2.
    case.leg['index'].pop(case.opens[case.i-4]+old.NS)
    assert arithmetic(old,case.leg,case.clock,row)['reason']=='MISSING_HISTORICAL_MINUTE'
