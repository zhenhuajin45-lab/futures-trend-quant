from pathlib import Path
from unittest.mock import Mock
import json
import pytest
from tools import paper_postclose_r7_release as r7
from test_paper_r6_release import layout


def fixture(tmp_path):
    client,rt,bundle,state,path=layout(tmp_path)
    staged=bundle/'client/_strategy_runtime/config/price_action_shadow.local.json'
    staged.parent.mkdir(parents=True);staged.write_text(json.dumps(r7.parameters()))
    plan=json.loads((bundle/'release_plan.local.json').read_text())
    plan.update(version=r7.VERSION,shadow_config_sha256=r7.base.prior.sha(staged),old_shadow_config_sha256=None)
    (bundle/'release_plan.local.json').write_text(json.dumps(plan))
    gate=json.loads((bundle/'validation_gate.local.json').read_text())
    gate.update(shadow_isolation_passed=True,windows_io_passed=True,full_day_audit_completed=True)
    (bundle/'validation_gate.local.json').write_text(json.dumps(gate))
    return client,rt,bundle,state,path


@pytest.mark.parametrize('version',[r7.VERSION,r7.SUPERVISION_PATCH])
def test_shadow_install_rollback_preserves_trading_state_and_never_clears_pause(tmp_path,monkeypatch,version):
    client,rt,bundle,state,path=fixture(tmp_path);before=path.read_bytes()
    plan=json.loads((bundle/'release_plan.local.json').read_text());plan['version']=version
    (bundle/'release_plan.local.json').write_text(json.dumps(plan))
    monkeypatch.setattr(r7.base,'inventory',lambda:[])
    monkeypatch.setattr(r7.base.prior,'flat_stopped',Mock(return_value=(rt,rt/'evidence',state,{})))
    monkeypatch.setattr(r7.base,'self_check',lambda *a:dict(self_contained_check='PASS'))
    r7.install(client,bundle)
    assert (rt/r7.CONFIG).exists() and path.read_bytes()==before
    installed=json.loads((bundle/'installed.local.json').read_text());assert installed['pause_retained'] and installed['version']==version
    r7.rollback(client,bundle)
    assert not (rt/r7.CONFIG).exists() and path.read_bytes()==before
    assert (client/'main.py').read_text()=='OLD\n'


@pytest.mark.parametrize('gate',['shadow_isolation_passed','windows_io_passed','full_day_audit_completed'])
def test_extra_validation_must_precede_any_client_mutation(tmp_path,monkeypatch,gate):
    client,rt,bundle,state,path=fixture(tmp_path)
    p=bundle/'validation_gate.local.json';data=json.loads(p.read_text());data[gate]=False;p.write_text(json.dumps(data))
    called=Mock();monkeypatch.setattr(r7.base,'install',called)
    with pytest.raises(ValueError,match='validation'):r7.install(client,bundle)
    called.assert_not_called();assert (client/'main.py').read_text()=='OLD\n'
