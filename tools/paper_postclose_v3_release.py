"""Same-core, flat-only execution upgrade; preserve every core checkpoint byte."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,json,shutil,subprocess,sys
from tools import paper_core_v2_release as prior
from futures_trend_quant.live.paper_broker import atomic
from futures_trend_quant.live.paper_config import frozen_core_digest,load_paper_config
from futures_trend_quant.live.paper_risk_controls import VERSION


def prepare(client,bundle,notification_v4=False):
    prior.prepare(client,bundle)
    plan=prior.read(bundle/'release_plan.local.json');stage=bundle/'client'
    path=stage/'_strategy_runtime/config/paper_observation_five.local.json'
    c=prior.read(path);c['execution_policy_version']=VERSION;atomic(path,c)
    if notification_v4:
        from futures_trend_quant.live.paper_cards import POLICY
        c['notification_policy_version']=POLICY;atomic(path,c)
    check=subprocess.run([sys.executable,'-X','utf8',str(stage/'main.py'),'--self-check','--check-config'],
                         capture_output=True,text=True,encoding='utf8',check=True)
    plan.update(execution_policy_version=VERSION,new_config_sha256=prior.sha(path),
                self_check=json.loads(check.stdout.strip().splitlines()[-1]),preserve_core_checkpoints=True,
                notification_policy_version=c.get('notification_policy_version'))
    atomic(bundle/'release_plan.local.json',plan)
    print(json.dumps(dict(prepared=True,execution_policy_version=VERSION,active_runtime_modified=False)))


def install(client,bundle):
    rt,ev,state,status=prior.flat_stopped(client)
    plan=prior.read(bundle/'release_plan.local.json');stage=bundle/'client'
    if Path(plan['active_client']).resolve()!=client.resolve():raise ValueError('release client mismatch')
    if prior.lfsha(client/'main.py')!=plan['old_main_sha256_lf'] or prior.sha(rt/'config/paper_observation_five.local.json')!=plan['old_config_sha256']:
        raise ValueError('active release changed')
    if prior.lfsha(stage/'main.py')!=plan['new_main_sha256_lf'] or prior.sha(stage/'_strategy_runtime/config/paper_observation_five.local.json')!=plan['new_config_sha256']:
        raise ValueError('staged release changed')
    if plan.get('execution_policy_version')!=VERSION or frozen_core_digest(rt)!=plan['new_core_sha256_lf']:
        raise ValueError('execution upgrade cannot change core/holding policy')
    gate=prior.read(bundle/'validation_gate.local.json')
    for field in ('targeted_tests_passed','native_quote_chain_passed','private_ledger_reconciled','core_unchanged'):
        if gate.get(field) is not True:raise ValueError('missing release gate: '+field)
    if gate.get('new_main_sha256_lf')!=plan['new_main_sha256_lf'] or gate.get('new_config_sha256')!=plan['new_config_sha256']:
        raise ValueError('release validation fingerprint mismatch')
    checkpoints={str(p.relative_to(rt)):prior.sha(p) for p in (rt/'state/paper_observation_five/products').glob('*/core_checkpoint.json')}
    if len(checkpoints)!=5 or status['ready_products']!=5:raise ValueError('five ready checkpoint books required')
    for rel in checkpoints:
        cp=prior.read(rt/rel)
        if cp['runtime'].get('core_contract')!='CLOSED_FACTS_V2' or not cp.get('seed_cache'):raise ValueError('unproved core continuity')
        seed=Path(cp['seed_cache']['path'])
        if not seed.resolve().is_relative_to(rt/'data/cache/paper_market') or prior.sha(seed)!=cp['seed_cache']['sha256']:
            raise ValueError('frozen seed changed')
    backup=bundle/'rollback';backup.mkdir()
    shutil.copyfile(client/'main.py',backup/'main.py')
    shutil.copyfile(rt/'config/paper_observation_five.local.json',backup/'paper_observation_five.local.json')
    shutil.copytree(rt/'state/paper_observation_five',backup/'state')
    shutil.copyfile(stage/'main.py',client/'main.py')
    shutil.copyfile(stage/'_strategy_runtime/config/paper_observation_five.local.json',rt/'config/paper_observation_five.local.json')
    for p in (rt/'state/paper_observation_five/products').glob('*/contract_pin.json'):
        pin=prior.read(p);pin['config_sha256']=plan['new_config_sha256'];atomic(p,pin)
    state.update(config_sha256=plan['new_config_sha256'],pause_new=True)
    atomic(rt/'state/paper_observation_five/execution.json',state)
    if any(prior.sha(rt/rel)!=value for rel,value in checkpoints.items()):raise RuntimeError('checkpoint altered; rollback required')
    subprocess.run([sys.executable,'-X','utf8',str(client/'main.py'),'--self-check','--check-config'],check=True)
    atomic(bundle/'installed.local.json',dict(at=datetime.now(timezone.utc).isoformat(),checkpoint_sha256=checkpoints,
        core_unchanged=True,pause_new=True,risk_state_preserved=True,execution_policy_version=VERSION,orders_sent=0))
    print(json.dumps(dict(installed=True,checkpoints_unchanged=5,pause_new=True,orders_sent=0)))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','install','resume']);p.add_argument('--client',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True);p.add_argument('--notification-v4',action='store_true')
    a=p.parse_args()
    if a.mode=='prepare':prepare(a.client.resolve(),a.bundle.resolve(),notification_v4=a.notification_v4)
    else:
        if a.notification_v4:raise ValueError('notification policy is chosen only at preparation')
        dict(install=install,resume=prior.resume)[a.mode](a.client.resolve(),a.bundle.resolve())
