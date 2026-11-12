import json,shutil
import pytest
from tools import paper_gm_r13_release as release
from futures_trend_quant.live.paper_broker import atomic
from test_r9_release import fixture as r9_fixture


def fixture(tmp_path,monkeypatch):
    client,rt,bundle,plan,state=r9_fixture(tmp_path,monkeypatch)
    profile=bundle/'client/_strategy_runtime'/release.PROFILE
    old=json.loads((rt/release.PROFILE).read_text());atomic(profile,dict(old,symbol_market_policy=release.POLICY,clock_sampler_policy=release.SAMPLER_POLICY,clock_startup_policy=release.STARTUP_POLICY,tq_input_wait_policy=release.INPUT_WAIT_POLICY,notice_evaluation_policy='NOTICE_CAUSAL_PREFILTER_V1_20261112',gm_consumed_input_policy='GM_CONSUMED_INPUT_CACHE_V1_20261112',gm_handoff_policy='GM_PRIORITY_HANDOFF_V1_20261112',clock_probe_retry_policy='CLOCK_DEGRADED_RETRY_V1_20261112'))
    plan.update(version=release.VERSION,new_config_sha256=release.r8.base.prior.sha(profile));atomic(bundle/'release_plan.local.json',plan)
    p=bundle/'validation_gate.local.json';gate=json.loads(p.read_text());gate['config_sha256']=plan['new_config_sha256'];atomic(p,gate)
    import tools.paper_gm_r13_migration as migration
    monkeypatch.setattr(migration,'validate',lambda *a: {})
    monkeypatch.setattr(migration,'apply',lambda *a: {})
    atomic(bundle/'gm_migration.local.json',{})
    gate=json.loads((bundle/'validation_gate.local.json').read_text());gate['migration_sha256']=release.r8.base.prior.sha(bundle/'gm_migration.local.json');atomic(bundle/'validation_gate.local.json',gate)
    monkeypatch.setattr(release,'source_hashes',lambda _: {})
    monkeypatch.setattr(release,'stopped',lambda plan:release.r8.base.stopped(plan))
    return client,rt,bundle,plan,state


def test_new_client_start_after_an_empty_prepared_inventory_is_still_blocked(tmp_path,monkeypatch):
    monkeypatch.setattr(release.r8.base,'stopped',lambda plan:None)
    plan=dict(active_client=str(tmp_path/'client'),host_pid_at_prepare=42,owned_at_prepare=[])
    rows=[dict(ProcessId=9001,ParentProcessId=1,CommandLine=str(tmp_path/'client/main.py'))]
    with pytest.raises(ValueError,match='9001'):release.stopped(plan,rows)
    assert release.stopped(plan,[dict(ProcessId=9002,ParentProcessId=1,CommandLine='another strategy')]) is None


@pytest.mark.parametrize('active_child',[False,True])
def test_completed_platform_cmd_wrapper_is_distinct_from_live_native_child(tmp_path,monkeypatch,active_child):
    monkeypatch.setattr(release.r8.base,'stopped',lambda plan:None)
    plan=dict(active_client=str(tmp_path/'client'),host_pid_at_prepare=42,owned_at_prepare=[])
    rows=[dict(ProcessId=9001,ParentProcessId=1,Name='cmd.exe',CommandLine=str(tmp_path/'client/main.py'))]
    rows.append(dict(ProcessId=9003,ParentProcessId=9001,Name='conhost.exe',CommandLine='console'))
    if active_child:
        rows.append(dict(ProcessId=9002,ParentProcessId=9001,Name='python.exe',CommandLine='spawn_main'))
        with pytest.raises(ValueError,match='9002'):release.stopped(plan,rows)
    else:assert release.stopped(plan,rows) is None


def test_r13_installs_only_after_gate_preserves_checkpoints_protection_and_all_ledgers(tmp_path,monkeypatch):
    client,rt,bundle,plan,state=fixture(tmp_path,monkeypatch)
    files={p:p.read_bytes() for p in (rt/'state').rglob('*') if p.is_file() and p.name not in ('execution.json','contract_pin.json')}
    receipt=release.install(client,bundle)
    assert all(p.read_bytes()==data for p,data in files.items())
    assert json.loads((rt/'state/paper_observation_five/execution.json').read_text())==dict(state,config_sha256=plan['new_config_sha256'])
    assert receipt['pins']==10 and receipt['pause_retained'] and not receipt['actual_host_loaded']


@pytest.mark.parametrize('case',['running','account','gate','scope','source'])
def test_r13_prevents_overwrite_on_running_or_unreviewed_handoff(tmp_path,monkeypatch,case):
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
def test_r13_preopen_inspection_copy_is_hash_bound_and_keeps_old_backup(tmp_path,monkeypatch,tamper):
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
        with pytest.raises(ValueError,match='R13_INSPECTION'):release.install(client,bundle)
        assert (client/'main.py').read_bytes()==before and old.read_text()=='# OLD'
    else:
        release.install(client,bundle);assert old.read_text()=='# NEW'
    assert backup.read_text()=='# OLD'


@pytest.mark.parametrize('tamper',[None,'log_prefix','checkpoint','execution'])
def test_final_account_refresh_after_hashing_preserves_evidence_and_rejects_other_changes(tmp_path,monkeypatch,tamper):
    client,rt,bundle,plan,state=fixture(tmp_path,monkeypatch)
    ev=rt/'evidence/paper_observation_five';ev.mkdir(exist_ok=True,parents=True)
    proof=ev/'controlled_handoff_snapshot.local.json';proof.write_text('{"before":true}')
    log=ev/'native_stages_reader.local.jsonl';log.write_bytes(b'OLD\n')
    checkpoint=rt/'state/paper_observation_five/protected_checkpoint.local.json';checkpoint.write_text('UNCHANGED')
    calls=[]
    def refresh():
        calls.append('FRESH_READ_ONLY')
        proof.write_text('{"after":true}')
        log.write_bytes(b'BROKEN\n' if tamper=='log_prefix' else b'OLD\nNEW\n')
        if tamper=='checkpoint':checkpoint.write_text('CHANGED')
        if tamper=='execution':
            p=rt/'state/paper_observation_five/execution.json';s=json.loads(p.read_text());s['pause_new']=False;atomic(p,s)
    before=(client/'main.py').read_bytes()
    if tamper:
        with pytest.raises(ValueError,match='HANDOFF'):release.install(client,bundle,refresh_handoff=refresh)
        assert (client/'main.py').read_bytes()==before
    else:
        receipt=release.install(client,bundle,refresh_handoff=refresh)
        assert receipt['pause_retained'] and log.read_bytes()==b'OLD\nNEW\n'
        assert checkpoint.read_text()=='UNCHANGED'
    assert calls==['FRESH_READ_ONLY']
    archive=next((bundle/'rollback').glob('final_handoff_*'))
    assert (archive/'native_stages_reader.local.jsonl').read_bytes()==b'OLD\n'


@pytest.mark.parametrize('case',['valid','new_fact','new_host','archive','readonly_prefix'])
def test_explicit_rollback_restores_exact_added_checkpoint_only_before_new_facts(tmp_path,monkeypatch,case):
    client,rt,bundle,plan,state=fixture(tmp_path,monkeypatch)
    backup=bundle/'rollback';backup.mkdir(exist_ok=True)
    shutil.copyfile(client/'main.py',backup/'main.py');shutil.copyfile(rt/release.PROFILE,backup/'profile.private.json')
    log=rt/'evidence/paper_observation_five/native_stages_reader.local.jsonl';log.write_bytes(b'OLD_READ\n')
    release.install(client,bundle);changes={};originals={}
    for cs in ('DCE.M','SHFE.AU','CZCE.OI','DCE.Y','DCE.P'):
        cp=rt/'state/paper_observation_five/products'/cs.replace('.','_')/'core_checkpoint.json';originals[cp]=cp.read_bytes()
        before=release.r8.base.prior.sha(cp);atomic(cp,dict(json.loads(cp.read_text()),gm_input_cache={'SYNTHETIC':'PROVEN'}))
        changes[cp.relative_to(rt).as_posix()]=dict(before_sha256=before,after_sha256=release.r8.base.prior.sha(cp))
    atomic(bundle/'gm_migrated.local.json',dict(checkpoints=changes))
    log.write_bytes(b'BAD_PREFIX\n' if case=='readonly_prefix' else b'OLD_READ\nFRESH_READ\n')
    if case=='new_fact':
        p=rt/'state/paper_observation_five/execution.json';atomic(p,dict(json.loads(p.read_text()),used_signals=['NEW_FACT']))
    if case=='new_host':atomic(rt/'evidence/paper_observation_five/gm_platform_host.local.json',{'pid':43})
    if case=='archive':(backup/'state_at_install/products/DCE_M/core_checkpoint.json').write_bytes(b'BAD')
    if case!='valid':
        with pytest.raises(ValueError):release.rollback_before_user_start(client,bundle)
        assert release.r8.base.prior.lfsha(client/'main.py')==plan['new_main_sha256_lf']
    else:
        result=release.rollback_before_user_start(client,bundle)
        assert result['rollback'] and result['pause_retained']
        assert all(p.read_bytes()==raw for p,raw in originals.items())
        assert json.loads((rt/'state/paper_observation_five/execution.json').read_text())==state
        assert log.read_bytes()==b'OLD_READ\nFRESH_READ\n'
