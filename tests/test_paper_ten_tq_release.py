"""State-preserving, stopped-only ten-pool installation; no real client writes."""
from pathlib import Path
import json
import pytest
from tools import paper_ten_tq_release as release
from futures_trend_quant.live.paper_tq import atomic,sha
from futures_trend_quant.live.paper_products import OILS_POOL,TEN_POOL,TQ_ADDED


def fixture(tmp_path,monkeypatch):
    client=tmp_path/'client';rt=client/'_strategy_runtime';bundle=tmp_path/'bundle';staged=bundle/'client/_strategy_runtime'
    profile=Path('config/paper_observation_five.local.json');state_dir=rt/'state/paper_observation_five'
    client.mkdir();bundle.mkdir();(bundle/'client').mkdir();(bundle/'rollback').mkdir()
    (client/'main.py').write_text('OLD',encoding='utf8');(bundle/'client/main.py').write_text('NEW',encoding='utf8')
    atomic(rt/profile,dict(universe=list(OILS_POOL)));atomic(staged/profile,dict(universe=list(TEN_POOL)))
    state=dict(config_sha256=sha(rt/profile),account_id='SYNTHETIC_ONLY',pause_new=True,pause_metadata=dict(reason='KEEP'),halt_reason='',
        intents={},used_signals=['RETAIN'],peak_nav=1000,day_nav=900,loss_cooldown=dict(through='KEEP'))
    atomic(state_dir/'execution.json',state);atomic(state_dir/'migration.local.json',dict(kind='OLD'))
    for cs in OILS_POOL:
        p=state_dir/'products'/cs.replace('.','_');atomic(p/'contract_pin.json',dict(symbol='SYNTHETIC',config_sha256=state['config_sha256']))
        atomic(p/'core_checkpoint.json',dict(runtime={'consumed':'KEEP'},last_eob='KEEP'))
    atomic(state_dir/'price_action_shadow/observer.json',dict(known_at='KEEP',policy='SHADOW'))
    atomic(rt/'evidence/paper_observation_five/gm_platform_host.local.json',dict(pid=42))
    rel='data/cache/new_tq/manifest.json';atomic(staged/rel,dict(source='SYNTHETIC_ONLY'))
    plan=dict(version=release.VERSION,active_client=str(client),seeds_qualified=True,prewarm_completed=True,host_pid_at_prepare=42,owned_at_prepare=[],
        old_main_sha256_lf=release.r8.base.prior.lfsha(client/'main.py'),old_config_sha256=sha(rt/profile),
        new_main_sha256_lf=release.r8.base.prior.lfsha(bundle/'client/main.py'),new_config_sha256=sha(staged/profile),
        private_inputs={rel:sha(staged/rel)},seed_manifest_sha256=sha(staged/rel))
    plan.update(new_state_files={},new_metadata_files={},prewarm_migrations={})
    symbols=['CZCE.MA701','DCE.eg2701','DCE.jd2612','CZCE.CJ701','SHFE.ag2702']
    for cs,symbol in zip(TQ_ADDED,symbols):
        key=cs.replace('.','_');cache=Path('data/cache/paper_market')/('SYNTHETIC_'+key)/'initial_m1.parquet'
        (staged/cache).parent.mkdir(parents=True);(staged/cache).write_bytes(b'SYNTHETIC_ONLY');plan['private_inputs'][str(cache)]=sha(staged/cache)
        dest=Path('state/paper_observation_five/products')/key
        atomic(staged/dest/'contract_pin.json',dict(symbol=symbol,config_sha256=plan['new_config_sha256']))
        atomic(staged/dest/'core_checkpoint.json',dict(runtime={'core_contract':'CLOSED_FACTS_V2'},last_eob='SYNTHETIC_CLOSED',seed_cache=dict(symbol=symbol,path=str(rt/cache),sha256=sha(staged/cache))))
        for name in ('core_checkpoint.json','contract_pin.json'):plan['new_state_files'][str(dest/name)]=sha(staged/dest/name)
        meta=Path('evidence/paper_observation_five/products')/key/'market_metadata.local.json';atomic(staged/meta,dict(symbol=symbol,csymbol=cs));plan['new_metadata_files'][str(meta)]=sha(staged/meta)
        plan['prewarm_migrations'][cs]=dict(last_eob='SYNTHETIC_CLOSED')
    atomic(bundle/'release_plan.local.json',plan)
    gate=dict(tests_passed=True,production_review_passed=True,native_py310_passed=True,host_py313_passed=True,real_feed_consumer_passed=True,
        main_sha256_lf=plan['new_main_sha256_lf'],config_sha256=plan['new_config_sha256'],seed_manifest_sha256=plan['seed_manifest_sha256'])
    atomic(bundle/'validation_gate.local.json',gate)
    monkeypatch.setattr(release.r8.base,'stopped',lambda p:None)
    monkeypatch.setattr(release.r8.base.prior,'flat_stopped',lambda c:(rt,rt/'evidence/paper_observation_five',json.loads((state_dir/'execution.json').read_text()),{}))
    monkeypatch.setattr(release.r8,'validate_active_sources',lambda c:None)
    monkeypatch.setattr(release.r8.base,'self_check',lambda c:dict(result='SYNTHETIC_PASS'))
    return client,rt,bundle,state


def test_install_keeps_existing_checkpoints_ledger_pause_and_known_at(tmp_path,monkeypatch):
    client,rt,bundle,state=fixture(tmp_path,monkeypatch)
    protected={p:sha(p) for p in (rt/'state').rglob('*.json') if p.name in ('core_checkpoint.json','observer.json')}
    release.install(client,bundle)
    assert (client/'main.py').read_text()=='NEW'
    after=json.loads((rt/'state/paper_observation_five/execution.json').read_text())
    assert {k:v for k,v in after.items() if k!='config_sha256'}=={k:v for k,v in state.items() if k!='config_sha256'}
    assert all(sha(p)==h for p,h in protected.items())
    assert json.loads((bundle/'installed.local.json').read_text())['actual_host_loaded'] is False


@pytest.mark.parametrize('block',['running','gate','host','private_conflict','new_state_conflict','no_pause','active_changed'])
def test_install_blocked_before_any_active_overwrite(tmp_path,monkeypatch,block):
    client,rt,bundle,state=fixture(tmp_path,monkeypatch)
    if block=='running':
        def fail(p):raise ValueError('USER_STOP_REQUIRED')
        monkeypatch.setattr(release.r8.base,'stopped',fail)
    elif block=='gate':
        p=bundle/'validation_gate.local.json';g=json.loads(p.read_text());g['native_py310_passed']=False;atomic(p,g)
    elif block=='host':atomic(rt/'evidence/paper_observation_five/gm_platform_host.local.json',dict(pid=43))
    elif block=='private_conflict':atomic(rt/'data/cache/new_tq/manifest.json',dict(unreviewed='KEEP'))
    elif block=='new_state_conflict':(rt/'state/paper_observation_five/products/DCE_JD').mkdir()
    elif block=='no_pause':state['pause_new']=False;atomic(rt/'state/paper_observation_five/execution.json',state)
    else:(client/'main.py').write_text('USER_EDIT',encoding='utf8')
    old=sha(client/'main.py');profile=sha(rt/'config/paper_observation_five.local.json')
    with pytest.raises(ValueError):release.install(client,bundle)
    assert sha(client/'main.py')==old and sha(rt/'config/paper_observation_five.local.json')==profile
