import json,shutil
import pytest
from tools import paper_symbol_r10_release as release
from futures_trend_quant.live.paper_broker import atomic
from test_r9_release import fixture as r9_fixture


def fixture(tmp_path,monkeypatch):
    client,rt,bundle,plan,state=r9_fixture(tmp_path,monkeypatch)
    profile=bundle/'client/_strategy_runtime'/release.PROFILE
    old=json.loads((rt/release.PROFILE).read_text());atomic(profile,dict(old,symbol_market_policy=release.POLICY))
    plan.update(version=release.VERSION,new_config_sha256=release.r8.base.prior.sha(profile));atomic(bundle/'release_plan.local.json',plan)
    p=bundle/'validation_gate.local.json';gate=json.loads(p.read_text());gate['config_sha256']=plan['new_config_sha256'];atomic(p,gate)
    monkeypatch.setattr(release,'source_hashes',lambda _: {})
    return client,rt,bundle,plan,state


def test_r10_installs_only_after_gate_preserves_checkpoints_protection_and_all_ledgers(tmp_path,monkeypatch):
    client,rt,bundle,plan,state=fixture(tmp_path,monkeypatch)
    files={p:p.read_bytes() for p in (rt/'state').rglob('*') if p.is_file() and p.name not in ('execution.json','contract_pin.json')}
    receipt=release.install(client,bundle)
    assert all(p.read_bytes()==data for p,data in files.items())
    assert json.loads((rt/'state/paper_observation_five/execution.json').read_text())==dict(state,config_sha256=plan['new_config_sha256'])
    assert receipt['pins']==10 and receipt['pause_retained'] and not receipt['actual_host_loaded']


@pytest.mark.parametrize('case',['running','account','gate','scope','source'])
def test_r10_prevents_overwrite_on_running_or_unreviewed_handoff(tmp_path,monkeypatch,case):
    client,rt,bundle,plan,state=fixture(tmp_path,monkeypatch)
    def reject(*a):raise ValueError('BLOCK')
    if case=='running':monkeypatch.setattr(release.r8.base,'stopped',reject)
    elif case=='account':monkeypatch.setattr(release.r8.base.prior,'flat_stopped',reject)
    elif case=='source':monkeypatch.setattr(release.r8,'validate_active_sources',reject)
    elif case=='gate':
        p=bundle/'validation_gate.local.json';d=json.loads(p.read_text());d['production_review_passed']=False;atomic(p,d)
    else:
        p=bundle/'client/_strategy_runtime'/release.PROFILE;d=json.loads(p.read_text());d['risk_pct']=.01;atomic(p,d)
    before=(client/'main.py').read_bytes()
    with pytest.raises(ValueError):release.install(client,bundle)
    assert (client/'main.py').read_bytes()==before


@pytest.mark.parametrize('tamper',[None,'gate','stage'])
def test_r10_preopen_inspection_copy_is_hash_bound_and_keeps_old_backup(tmp_path,monkeypatch,tamper):
    client,rt,bundle,plan,state=fixture(tmp_path,monkeypatch)
    old=client/'ops/night_inspect.py';new=bundle/'client/ops/night_inspect.py'
    old.parent.mkdir(exist_ok=True);new.parent.mkdir(exist_ok=True)
    old.write_text('# OLD');new.write_text('# NEW')
    backup=bundle/'rollback/night_inspect.py';backup.parent.mkdir(exist_ok=True);shutil.copyfile(old,backup)
    plan.update(old_inspection_sha256=release.r8.base.prior.sha(old),new_inspection_sha256=release.r8.base.prior.sha(new));atomic(bundle/'release_plan.local.json',plan)
    p=bundle/'validation_gate.local.json';gate=json.loads(p.read_text());gate['inspection_sha256']='BAD' if tamper=='gate' else plan['new_inspection_sha256'];atomic(p,gate)
    if tamper=='stage':new.write_text('# CHANGED')
    before=(client/'main.py').read_bytes()
    if tamper:
        with pytest.raises(ValueError,match='R10_INSPECTION'):release.install(client,bundle)
        assert (client/'main.py').read_bytes()==before and old.read_text()=='# OLD'
    else:
        release.install(client,bundle);assert old.read_text()=='# NEW'
    assert backup.read_text()=='# OLD'
