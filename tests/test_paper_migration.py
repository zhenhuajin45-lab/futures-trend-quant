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
