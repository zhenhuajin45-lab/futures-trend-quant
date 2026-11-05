import json
from copy import deepcopy
import pytest
from tools.paper_five_migrate import migrate
from test_paper_execution import ACCOUNT,snapshot


def setup(tmp_path):
    old=dict(account_id=ACCOUNT,core_policy_sha256_lf='CORE',base_strategy_sha256_lf='RULES',config_sha256='OLD')
    new=dict(old,config_sha256='NEW',universe=['DCE.M','SHFE.AU','DCE.EG','DCE.JD','CZCE.CJ'])
    ss=tmp_path/'source/state';se=tmp_path/'source/evidence';ts=tmp_path/'target/state';te=tmp_path/'target/evidence'
    ss.mkdir(parents=True);se.mkdir(parents=True)
    state=dict(account_id=ACCOUNT,config_sha256='OLD',intents={},pause_new=True,halt_reason='PERSIST_THIS',connectivity_passed=True,initial_nav=1000000)
    (ss/'execution.json').write_text(json.dumps(state));(ss/'core_checkpoint.json').write_text('{"checkpoint":true}')
    (ss/'contract_pin.json').write_text('{"config_sha256":"OLD","symbol":"DCE.m2701"}')
    (se/'market_metadata.local.json').write_text('{"symbol":"DCE.m2701"}')
    (se/'fills.csv').write_text('existing test ledger')
    return old,new,snapshot(),ss,ts,se,te


def test_handoff_preserves_risk_state_and_source_and_notification_outbox(tmp_path):
    args=setup(tmp_path);old,new,s,ss,ts,se,te=args
    before=(ss/'execution.json').read_bytes();(te/'notifications').mkdir(parents=True);(te/'notifications/sent.json').write_text('sent')
    receipt=migrate(*args);restored=json.loads((ts/'execution.json').read_text())
    assert restored['pause_new'] and restored['halt_reason']=='PERSIST_THIS'
    assert restored['initial_nav']==1000000 and restored['config_sha256']=='NEW' and receipt['connectivity_passed']
    assert (ss/'execution.json').read_bytes()==before and (te/'notifications/sent.json').read_text()=='sent'
    assert (te/'fills.csv').read_text()=='existing test ledger' and (ts/'migration.local.json').exists()
    assert json.loads((ts/'products/DCE_M/contract_pin.json').read_text())['config_sha256']=='NEW'
    with pytest.raises(ValueError,match='already exists'):migrate(*args)


@pytest.mark.parametrize('problem',['foreign','active','unowned','mismatch'])
def test_handoff_rejects_uncertain_or_foreign_broker_truth_before_writes(tmp_path,problem):
    args=list(setup(tmp_path));s=args[2]
    if problem=='foreign':s['cash']['account_id']='OTHER'
    if problem=='active':s['active']=[dict(account_id=ACCOUNT)]
    if problem=='unowned':s['orders']=[dict(account_id=ACCOUNT,cl_ord_id='UNOWNED')]
    if problem=='mismatch':s['positions']=[dict(account_id=ACCOUNT,symbol='DCE.m2701',side=1,volume=1)]
    with pytest.raises(ValueError):migrate(*args)
    assert not (args[4]/'execution.json').exists()


def test_only_qualified_matching_preflight_state_can_be_imported(tmp_path):
    from tools.paper_five_migrate import qualified_preflight
    from futures_trend_quant.live.paper_products import FIVE_POOL
    p=tmp_path/'data/cache/five_preflight/run/evidence';p.mkdir(parents=True)
    status=dict(phase='READONLY_PREFLIGHT_PASS',config_sha256='NEW',ready_products=5,products={})
    for cs in FIVE_POOL:
        key=cs.replace('.','_');st=p.parent/'state/products'/key;ev=p/'products'/key
        st.mkdir(parents=True);ev.mkdir(parents=True)
        symbol=cs+'ACTUAL'
        (st/'core_checkpoint.json').write_text(json.dumps(dict(last_eob='CLOSED')))
        (st/'contract_pin.json').write_text(json.dumps(dict(config_sha256='NEW',symbol=symbol)))
        (ev/'market_metadata.local.json').write_text(json.dumps(dict(csymbol=cs,symbol=symbol)))
        status['products'][cs]=dict(ready=True,warmup=dict(missing_m1=0,qualified=True),symbol=symbol,core=dict(last_eob='CLOSED'))
    (p/'status.json').write_text(json.dumps(status));(p/'run_manifest.json').write_text(json.dumps(dict(readonly=True,config_sha256='NEW')))
    new=dict(config_sha256='NEW',universe=list(FIVE_POOL))
    assert set(qualified_preflight(new,p,root=tmp_path))==set(FIVE_POOL)
    with pytest.raises(ValueError):qualified_preflight(dict(new,config_sha256='OTHER'),p,root=tmp_path)
    status['products']['DCE.JD']['warmup']['missing_m1']=1;(p/'status.json').write_text(json.dumps(status))
    with pytest.raises(ValueError):qualified_preflight(new,p,root=tmp_path)


def test_warmup_import_does_not_override_original_beanmeal_checkpoint(tmp_path):
    args=setup(tmp_path)
    proof={'DCE.M':dict(files={'core_checkpoint.json':b'NO'}),'DCE.JD':dict(files={'core_checkpoint.json':b'VERIFIED','contract_pin.json':b'PIN','market_metadata.local.json':b'META'})}
    migrate(*args,preflight=proof)
    assert (args[4]/'products/DCE_M/core_checkpoint.json').read_text()=='{"checkpoint":true}'
    assert (args[4]/'products/DCE_JD/core_checkpoint.json').read_bytes()==b'VERIFIED'
