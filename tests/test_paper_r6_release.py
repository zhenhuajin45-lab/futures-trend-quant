"""Temporary-file release guards; no actual process control or client writes."""
from pathlib import Path
import json
from unittest.mock import Mock
import pytest
from tools import paper_execution_r6_release as release


def test_owned_descendants_and_pid_reuse_are_distinguished(monkeypatch):
    rows=[dict(ProcessId=10,ParentProcessId=1,CreationDate='A'),dict(ProcessId=11,ParentProcessId=10,CreationDate='B'),dict(ProcessId=12,ParentProcessId=11,CreationDate='C'),dict(ProcessId=99,ParentProcessId=2,CreationDate='D')]
    owned=release.owned_processes(10,rows);assert {x['ProcessId'] for x in owned}=={10,11,12}
    monkeypatch.setattr(release,'inventory',lambda:rows)
    with pytest.raises(ValueError,match='USER_STOP_REQUIRED'):release.stopped({'owned_at_prepare':owned})
    monkeypatch.setattr(release,'inventory',lambda:[dict(rows[0],CreationDate='REUSED')])
    release.stopped({'owned_at_prepare':owned})


def layout(tmp_path):
    client=tmp_path/'client';rt=client/'_strategy_runtime';bundle=tmp_path/'release';stage=bundle/'client'
    (rt/'config').mkdir(parents=True);(rt/'state/paper_observation_five/products/X').mkdir(parents=True)
    stage.mkdir(parents=True);(client/'main.py').write_text('OLD\n');(stage/'main.py').write_text('NEW\n')
    (rt/'config/paper_observation_five.local.json').write_text('{}')
    state=dict(pause_new=True,pause_metadata={'reason':'SERVICE_INCIDENT_REQUIRES_REVIEW'},intents={'retained':{'filled':1}})
    path=rt/'state/paper_observation_five/execution.json';path.write_text(json.dumps(state));(path.parent/'products/X/core_checkpoint.json').write_text('CHECKPOINT')
    plan=dict(active_client=str(client),new_main_sha256_lf=release.prior.lfsha(stage/'main.py'),old_main_sha256_lf=release.prior.lfsha(client/'main.py'),config_sha256=release.prior.sha(rt/'config/paper_observation_five.local.json'),owned_at_prepare=[],host_python='ISOLATED_TEST',private_sdk_files={})
    (bundle/'release_plan.local.json').write_text(json.dumps(plan));gate={k:True for k in ['tests_passed','real_sdk_tick_passed','async_native_chain_passed','core_unchanged','production_review_passed']};gate['main_sha256_lf']=plan['new_main_sha256_lf'];(bundle/'validation_gate.local.json').write_text(json.dumps(gate))
    return client,rt,bundle,state,path


def test_install_and_code_rollback_preserve_exact_ledger_checkpoint_pause_bytes(tmp_path,monkeypatch):
    client,rt,bundle,state,path=layout(tmp_path);before=path.read_bytes()
    monkeypatch.setattr(release,'inventory',lambda:[]);flat=Mock(return_value=(rt,rt/'evidence',state,{}));monkeypatch.setattr(release.prior,'flat_stopped',flat)
    monkeypatch.setattr(release,'self_check',lambda *a:{'PASS':True})
    release.install(client,bundle)
    assert path.read_bytes()==before and (client/'main.py').read_text()=='NEW\n'
    result=json.loads((bundle/'installed.local.json').read_text());assert result['pause_retained'] and not result['actual_host_loaded']
    release.rollback(client,bundle)
    assert path.read_bytes()==before and (client/'main.py').read_text()=='OLD\n'
    assert (path.parent/'products/X/core_checkpoint.json').read_text()=='CHECKPOINT' and flat.call_count==2


def test_failed_validation_cannot_touch_active_main(tmp_path,monkeypatch):
    client,rt,bundle,state,path=layout(tmp_path);gate=bundle/'validation_gate.local.json';x=json.loads(gate.read_text());x['async_native_chain_passed']=False;gate.write_text(json.dumps(x));flat=Mock();monkeypatch.setattr(release.prior,'flat_stopped',flat)
    with pytest.raises(ValueError,match='validation'):release.install(client,bundle)
    flat.assert_not_called();assert (client/'main.py').read_text()=='OLD\n'
