from pathlib import Path
import json
from types import SimpleNamespace
import pytest
from tools import paper_postclose_v3_release as release
from futures_trend_quant.live.paper_risk_controls import VERSION


def fixture(tmp_path,monkeypatch):
    client=tmp_path/'client';rt=client/'_strategy_runtime';st=rt/'state/paper_observation_five';ev=rt/'evidence/paper_observation_five'
    bundle=tmp_path/'bundle';stage=bundle/'client';config=Path('_strategy_runtime/config/paper_observation_five.local.json')
    def put(path,row):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(row),encoding='utf8')
    put(client/'main.py',{'OLD':'main'});put(stage/'main.py',{'NEW':'main'})
    put(client/config,{'profile':'old'});put(stage/config,{'profile':'new'})
    cps={}
    for n in range(5):
        seed=rt/'data/cache/paper_market'/str(n)/'seed';seed.parent.mkdir(parents=True);seed.write_bytes(b'FROZEN')
        cp=st/'products'/str(n)/'core_checkpoint.json'
        put(cp,dict(runtime=dict(core_contract='CLOSED_FACTS_V2',tombstone='PRESERVE'),seed_cache=dict(path=str(seed),sha256=release.prior.sha(seed))))
        put(cp.with_name('contract_pin.json'),dict(config_sha256='OLD',symbol='SYNTHETIC'))
        cps[str(cp)]=cp.read_bytes()
    state=dict(halt_reason='',pause_new=True,day_nav=1,peak_nav=2,used_signals=['USED'],intents={},exec_ids=['EXEC'],core_v2_resume_authorizations={'old':'consumed'})
    put(st/'execution.json',state)
    monkeypatch.setattr(release.prior,'flat_stopped',lambda _: (rt,ev,state,dict(ready_products=5)))
    monkeypatch.setattr(release,'frozen_core_digest',lambda _:'CORE')
    monkeypatch.setattr(release.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0))
    plan=dict(active_client=str(client),old_main_sha256_lf=release.prior.lfsha(client/'main.py'),new_main_sha256_lf=release.prior.lfsha(stage/'main.py'),
              old_config_sha256=release.prior.sha(client/config),new_config_sha256=release.prior.sha(stage/config),
              new_core_sha256_lf='CORE',execution_policy_version=VERSION)
    put(bundle/'release_plan.local.json',plan)
    gate=dict(targeted_tests_passed=True,native_quote_chain_passed=True,private_ledger_reconciled=True,core_unchanged=True,
              new_main_sha256_lf=plan['new_main_sha256_lf'],new_config_sha256=plan['new_config_sha256'])
    put(bundle/'validation_gate.local.json',gate)
    return client,bundle,state,cps,put


def test_same_core_install_preserves_facts_risk_dedup_and_authorizations(tmp_path,monkeypatch):
    client,bundle,state,cps,put=fixture(tmp_path,monkeypatch)
    release.install(client,bundle)
    assert all(Path(p).read_bytes()==before for p,before in cps.items())
    after=release.prior.read(client/'_strategy_runtime/state/paper_observation_five/execution.json')
    for key in ('day_nav','peak_nav','used_signals','exec_ids','core_v2_resume_authorizations'):assert after[key]==state[key]
    assert after['pause_new'] is True and after['halt_reason']==''
    assert (bundle/'rollback/state/execution.json').exists()


@pytest.mark.parametrize('change',['profile','main','gate','core','seed'])
def test_install_rejects_unverified_or_changed_release(tmp_path,monkeypatch,change):
    client,bundle,state,cps,put=fixture(tmp_path,monkeypatch)
    before=(client/'main.py').read_bytes()
    if change=='profile':put(bundle/'client/_strategy_runtime/config/paper_observation_five.local.json',{'changed':1})
    if change=='main':put(bundle/'client/main.py',{'changed':1})
    if change=='gate':put(bundle/'validation_gate.local.json',{})
    if change=='core':monkeypatch.setattr(release,'frozen_core_digest',lambda _:'DIFFERENT')
    if change=='seed':(client/'_strategy_runtime/data/cache/paper_market/0/seed').write_bytes(b'REVISED')
    with pytest.raises(ValueError):release.install(client,bundle)
    assert (client/'main.py').read_bytes()==before and not (bundle/'rollback').exists()
