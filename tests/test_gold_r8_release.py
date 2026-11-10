from pathlib import Path
import json
from unittest.mock import Mock
import pytest
from tools import paper_gold_r8_release as r8


def put(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),encoding='utf8')


def layout(tmp_path,monkeypatch):
    c=tmp_path/'client';rt=c/'_strategy_runtime';b=tmp_path/'bundle';ev=rt/'evidence/paper_observation_five'
    c.mkdir();(c/'main.py').write_text('OLD\n');stage=b/'client';stage.mkdir(parents=True);(stage/'main.py').write_text('NEW\n')
    active=rt/'config/paper_observation_five.local.json';new=stage/'_strategy_runtime/config/paper_observation_five.local.json'
    put(active,{'policy':'OLD'});put(new,{'policy':'NEW'})
    state={'config_sha256':r8.base.prior.sha(active),'pause_new':True,'halt_reason':'','intents':{'old':{'status':3}},'candidate_status':{'X':{'status':'CONSUMED'}},'pause_metadata':{'reason':'GM_HOST_SHUTDOWN'}}
    put(rt/'state/paper_observation_five/execution.json',state)
    for n in range(5):
        put(rt/f'state/paper_observation_five/products/P{n}/core_checkpoint.json',{'facts':{'invalidated':{'X':'TOMBSTONE'}},'last_eob':'EXACT'})
        put(rt/f'state/paper_observation_five/products/P{n}/contract_pin.json',{'symbol':str(n),'config_sha256':state['config_sha256']})
    put(ev/'gm_platform_host.local.json',{'pid':42})
    backup=b/'rollback';backup.mkdir();(backup/'main.py').write_text('OLD\n');put(backup/'profile.private.json',{'policy':'OLD'})
    plan={'version':r8.VERSION,'active_client':str(c),'host_pid_at_prepare':42,'owned_at_prepare':[],
        'old_main_sha256_lf':r8.base.prior.lfsha(c/'main.py'),'new_main_sha256_lf':r8.base.prior.lfsha(stage/'main.py'),
        'old_config_sha256':r8.base.prior.sha(active),'new_config_sha256':r8.base.prior.sha(new),'new_core_sha256_lf':'NEW_CORE'}
    put(b/'release_plan.local.json',plan)
    gates={k:True for k in ('tests_passed','production_review_passed','native_py310_passed','host_py313_passed','historical_audit_completed')}
    gates.update(main_sha256_lf=plan['new_main_sha256_lf'],config_sha256=plan['new_config_sha256'],core_sha256_lf='NEW_CORE');put(b/'validation_gate.local.json',gates)
    monkeypatch.setattr(r8.base,'inventory',lambda:[])
    monkeypatch.setattr(r8,'validate_active_sources',lambda _:None)
    monkeypatch.setattr(r8.base.prior,'flat_stopped',lambda _: (rt,ev,r8.base.prior.read(rt/'state/paper_observation_five/execution.json'),{}))
    return c,rt,b,ev,state


def test_install_and_explicit_same_handoff_rollback_preserve_checkpoint_and_ledger(tmp_path,monkeypatch):
    c,rt,b,ev,state=layout(tmp_path,monkeypatch)
    cps={p:p.read_bytes() for p in (rt/'state').rglob('core_checkpoint.json')}
    r8.install(c,b)
    after=r8.base.prior.read(rt/'state/paper_observation_five/execution.json');after.pop('config_sha256');state.pop('config_sha256')
    assert after==state and all(p.read_bytes()==raw for p,raw in cps.items())
    assert (b/'rollback/state_at_install').exists()
    from futures_trend_quant.observation.price_action.models import digest,DEFAULTS
    pa=rt/'state/paper_observation_five/price_action_shadow/observer.json'
    put(pa,{'policy':'PRICE_ACTION_SHADOW_V2_WINDOW_COVERAGE_20261111','parameters_sha256':digest(DEFAULTS),'products':{'known_at':'PRESERVED'},'emitted':{'OLD':'CLOCK'}})
    with pytest.raises(ValueError,match='rollback preparation'):r8.install(c,b,rollback=True)
    r8.prepare_rollback(c,b);r8.install(c,b,rollback=True);assert (c/'main.py').read_text()=='OLD\n'
    migrated=r8.base.prior.read(pa);assert migrated['policy']=='PRICE_ACTION_SHADOW_V1_20261110' and migrated['products']['known_at']=='PRESERVED' and migrated['emitted']=={'OLD':'CLOCK'}
    assert all(p.read_bytes()==raw for p,raw in cps.items())


@pytest.mark.parametrize('block',['running','account','changed_host','gate','changed_source','changed_runtime'])
def test_all_handoff_blocks_precede_mutation(tmp_path,monkeypatch,block):
    c,rt,b,ev,state=layout(tmp_path,monkeypatch);before=(c/'main.py').read_bytes()
    if block=='running':monkeypatch.setattr(r8.base,'stopped',Mock(side_effect=ValueError('USER_STOP_REQUIRED')))
    if block=='account':monkeypatch.setattr(r8.base.prior,'flat_stopped',Mock(side_effect=ValueError('ACCOUNT_UNKNOWN')))
    if block=='changed_host':put(ev/'gm_platform_host.local.json',{'pid':43})
    if block=='gate':put(b/'validation_gate.local.json',{})
    if block=='changed_source':(b/'client/main.py').write_text('MUTATED')
    if block=='changed_runtime':monkeypatch.setattr(r8,'validate_active_sources',Mock(side_effect=ValueError('OPERATOR_SOURCE_EDIT')))
    with pytest.raises(ValueError):r8.install(c,b)
    assert (c/'main.py').read_bytes()==before
