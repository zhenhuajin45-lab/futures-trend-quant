"""Rollback must retain current execution history, never rewind to release backups."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from tools import paper_v5_release as release


def setup(tmp_path,monkeypatch):
    client=tmp_path/'client';rt=client/'_strategy_runtime';state_dir=rt/'state/paper_observation_five'
    bundle=tmp_path/'bundle';backup=bundle/'rollback'
    for p in (rt/'config',state_dir/'products/DCE_M',backup):p.mkdir(parents=True)
    (client/'main.py').write_text('# new\n');(backup/'main.py').write_text('# old\n')
    profile=rt/'config/paper_observation_five.local.json';profile.write_text('{"version":"new"}')
    (backup/'profile.private.json').write_text('{"version":"old"}')
    cp=state_dir/'products/DCE_M/core_checkpoint.json';cp.write_text('{"used_evidence_ids":["NEW_FILL"]}')
    (state_dir/'products/DCE_M/contract_pin.json').write_text('{}')
    state=dict(pause_new=False,halt_reason='',intents={'NEW_FILL':{'filled':1}},due_events={'EXIT_DONE':{'completed':True}})
    (state_dir/'execution.json').write_text(json.dumps(state))
    plan=dict(active_client=str(client),new_main_sha256_lf=release.prior.lfsha(client/'main.py'),old_main_sha256_lf=release.prior.lfsha(backup/'main.py'),old_config_sha256=release.prior.sha(backup/'profile.private.json'))
    (bundle/'release_plan.local.json').write_text(json.dumps(plan))
    monkeypatch.setattr(release.prior,'flat_stopped',lambda c:(rt,rt/'evidence',json.loads((state_dir/'execution.json').read_text()),{}))
    monkeypatch.setattr(release.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0))
    return client,bundle,state_dir,cp


def test_rollback_retains_current_fills_and_checkpoint(tmp_path,monkeypatch):
    client,bundle,state_dir,cp=setup(tmp_path,monkeypatch);checkpoint=cp.read_bytes()
    release.rollback(client,bundle)
    actual=json.loads((state_dir/'execution.json').read_text())
    assert actual['intents']=={'NEW_FILL':{'filled':1}}
    assert actual['due_events']=={'EXIT_DONE':{'completed':True}}
    assert actual['pause_new'] is True and cp.read_bytes()==checkpoint
    assert (client/'main.py').read_text()=='# old\n'


def test_rollback_rejects_unexpected_running_version_without_mutation(tmp_path,monkeypatch):
    client,bundle,state_dir,cp=setup(tmp_path,monkeypatch)
    (client/'main.py').write_text('# another version\n');before=(state_dir/'execution.json').read_bytes()
    with pytest.raises(ValueError,match='unexpected current release'):release.rollback(client,bundle)
    assert (state_dir/'execution.json').read_bytes()==before
    assert not (bundle/'before_rollback').exists()
