import json
from pathlib import Path
import pytest
from tools import paper_postclose_r9_release as r
from futures_trend_quant.live.paper_broker import atomic
from futures_trend_quant.live.paper_products import TEN_POOL


def fixture(tmp_path,monkeypatch):
    client=tmp_path/'client';rt=client/'_strategy_runtime';bundle=tmp_path/'bundle';stage=bundle/'client/_strategy_runtime'
    atomic(client/'main.py',{'OLD':1});atomic(bundle/'client/main.py',{'NEW':1})
    old=dict(universe=list(TEN_POOL),entry_policy_version='PAPER_ENTRY_V5',risk_pct=.0025)
    atomic(rt/r.PROFILE,old);atomic(stage/r.PROFILE,dict(old,gm_closed_m1_policy=r.POLICY))
    st=rt/'state/paper_observation_five';ev=rt/'evidence/paper_observation_five'
    state=dict(config_sha256=r.r8.base.prior.sha(rt/r.PROFILE),pause_new=True,halt_reason='',intents={'OLD':{'status':3}},used_signals=['KEEP'],meta={'OLD':'KEEP'},loss_cooldown={'until':'KEEP'})
    atomic(st/'execution.json',state);atomic(ev/'gm_platform_host.local.json',{'pid':42})
    for cs in TEN_POOL:
        p=st/'products'/cs.replace('.','_');atomic(p/'contract_pin.json',dict(symbol=cs,config_sha256=state['config_sha256']))
        atomic(p/'core_checkpoint.json',dict(runtime={'used_ids':['KEEP']},last_eob='ORIGINAL',seed_cache={'path':'FROZEN'}))
    atomic(st/'price_action_shadow/observer.json',{'known_at':'ORIGINAL','policy':'SHADOW','events':['KEEP']})
    atomic(ev/'ledger.jsonl',{'exact':'ORIGINAL'})
    plan=dict(version=r.VERSION,active_client=str(client),host_pid_at_prepare=42,private_inputs={},new_core_sha256_lf='SAME_CORE',
        old_main_sha256_lf=r.r8.base.prior.lfsha(client/'main.py'),new_main_sha256_lf=r.r8.base.prior.lfsha(bundle/'client/main.py'),
        old_config_sha256=state['config_sha256'],new_config_sha256=r.r8.base.prior.sha(stage/r.PROFILE))
    atomic(bundle/'release_plan.local.json',plan)
    gate={k:True for k in ('tests_passed','production_review_passed','native_py310_passed','host_py313_passed','historical_audit_completed','rollback_prepared')}
    gate.update({k:plan['new_'+k] for k in ('main_sha256_lf','config_sha256','core_sha256_lf')});atomic(bundle/'validation_gate.local.json',gate)
    monkeypatch.setattr(r.r8.base,'stopped',lambda _:None)
    monkeypatch.setattr(r.r8.base.prior,'flat_stopped',lambda _:(rt,ev,json.loads((st/'execution.json').read_text()),{}))
    monkeypatch.setattr(r.r8,'validate_active_sources',lambda _:None)
    monkeypatch.setattr(r.r8.base,'self_check',lambda _:dict(result='SYNTHETIC'))
    monkeypatch.setattr(r,'source_hashes',lambda _: {})
    return client,rt,bundle,plan,state


def test_r9_install_preserves_ten_checkpoints_all_ledger_pause_and_shadow_known_at(tmp_path,monkeypatch):
    client,rt,bundle,plan,state=fixture(tmp_path,monkeypatch)
    protected={p:p.read_bytes() for p in rt.rglob('*') if p.is_file() and p.name not in ('execution.json','contract_pin.json',r.PROFILE.name)}
    receipt=r.install(client,bundle)
    assert json.loads((rt/'state/paper_observation_five/execution.json').read_text())==dict(state,config_sha256=plan['new_config_sha256'])
    assert all(p.read_bytes()==raw for p,raw in protected.items())
    assert receipt['pins']==10 and receipt['pause_retained'] and not receipt['actual_host_loaded'] and receipt['orders_sent']==0
    assert (bundle/'rollback/state_at_install/price_action_shadow/observer.json').exists()


@pytest.mark.parametrize('new_fact',[False,True])
def test_explicit_before_start_rollback_never_restores_old_ledger_over_new_facts(tmp_path,monkeypatch,new_fact):
    import shutil
    client,rt,bundle,plan,state=fixture(tmp_path,monkeypatch)
    (bundle/'rollback').mkdir();shutil.copyfile(client/'main.py',bundle/'rollback/main.py');shutil.copyfile(rt/r.PROFILE,bundle/'rollback/profile.private.json')
    r.install(client,bundle)
    if new_fact:
        p=rt/'state/paper_observation_five/price_action_shadow/observer.json';atomic(p,dict(known_at='NEW_TRUE_CLOCK'))
        with pytest.raises(ValueError,match='REQUIRES_REVIEW'):r.rollback_before_user_start(client,bundle)
        assert r.r8.base.prior.lfsha(client/'main.py')==plan['new_main_sha256_lf']
        assert json.loads(p.read_text())['known_at']=='NEW_TRUE_CLOCK'
    else:
        receipt=r.rollback_before_user_start(client,bundle)
        assert receipt['rollback'] and receipt['pause_retained']
        assert json.loads((rt/'state/paper_observation_five/execution.json').read_text())==state
        assert r.r8.base.prior.lfsha(client/'main.py')==plan['old_main_sha256_lf']


@pytest.mark.parametrize('case',['running','account','gate','fingerprint','profile_scope','pin','source'])
def test_r9_rejects_before_any_actual_overwrite(tmp_path,monkeypatch,case):
    client,rt,bundle,plan,state=fixture(tmp_path,monkeypatch)
    def fail(*a):raise ValueError('EXPECTED_BLOCK')
    if case=='running':monkeypatch.setattr(r.r8.base,'stopped',fail)
    elif case=='account':monkeypatch.setattr(r.r8.base.prior,'flat_stopped',fail)
    elif case=='gate':
        p=bundle/'validation_gate.local.json';d=json.loads(p.read_text());d['native_py310_passed']=False;atomic(p,d)
    elif case=='fingerprint':
        p=bundle/'validation_gate.local.json';d=json.loads(p.read_text());d['main_sha256_lf']='OTHER';atomic(p,d)
    elif case=='profile_scope':
        p=bundle/'client/_strategy_runtime'/r.PROFILE;d=json.loads(p.read_text());d['risk_pct']=.01;atomic(p,d)
        plan['new_config_sha256']=r.r8.base.prior.sha(p);atomic(bundle/'release_plan.local.json',plan)
        g=json.loads((bundle/'validation_gate.local.json').read_text());g['config_sha256']=plan['new_config_sha256'];atomic(bundle/'validation_gate.local.json',g)
    elif case=='pin':atomic(rt/'state/paper_observation_five/products/DCE_M/contract_pin.json',{'config_sha256':'OTHER'})
    else:monkeypatch.setattr(r.r8,'validate_active_sources',fail)
    before={p:p.read_bytes() for p in [client/'main.py',rt/r.PROFILE,rt/'state/paper_observation_five/execution.json']}
    with pytest.raises(ValueError):r.install(client,bundle)
    assert all(p.read_bytes()==raw for p,raw in before.items())
