"""Flat-only, fingerprinted V5 release. Does not start processes or clear protections."""
from pathlib import Path
import argparse,json,shutil,subprocess,sys
from datetime import datetime,timezone
from tools import paper_core_v2_release as prior
from futures_trend_quant.live.paper_broker import atomic


def prepare(client,bundle,entry=False):
    prior.prepare(client,bundle)
    plan=prior.read(bundle/'release_plan.local.json');stage=bundle/'client'
    profile=stage/'_strategy_runtime/config/paper_observation_five.local.json'
    c=prior.read(profile);c['execution_policy_version']='PAPER_EXEC_V5'
    if entry:c['entry_policy_version']='PAPER_ENTRY_V5'
    atomic(profile,c)
    check=subprocess.run([sys.executable,'-X','utf8',str(stage/'main.py'),'--self-check','--check-config'],capture_output=True,text=True,encoding='utf-8',check=True)
    plan.update(execution_policy_version='PAPER_EXEC_V5',entry_policy_version=c.get('entry_policy_version'),new_config_sha256=prior.sha(profile),self_check=json.loads(check.stdout.strip().splitlines()[-1]),preserve_core_checkpoints=True)
    atomic(bundle/'release_plan.local.json',plan)
    print(json.dumps(dict(staged=True,entry_v5=entry,main_sha256_lf=plan['new_main_sha256_lf'],active_modified=False)))


def install(client,bundle):
    rt,ev,state,status=prior.flat_stopped(client)
    plan=prior.read(bundle/'release_plan.local.json');stage=bundle/'client'
    if Path(plan['active_client']).resolve()!=client.resolve():raise ValueError('wrong client')
    for path,key in [(client/'main.py','old_main_sha256_lf'),(stage/'main.py','new_main_sha256_lf')]:
        if prior.lfsha(path)!=plan[key]:raise ValueError('main fingerprint changed')
    for path,key in [(rt/'config/paper_observation_five.local.json','old_config_sha256'),(stage/'_strategy_runtime/config/paper_observation_five.local.json','new_config_sha256')]:
        if prior.sha(path)!=plan[key]:raise ValueError('config fingerprint changed')
    gate=prior.read(bundle/'validation_gate.local.json')
    if gate.get('main_sha256_lf')!=plan['new_main_sha256_lf'] or not gate.get('tests_passed') or not gate.get('sdk_compatibility_passed'):raise ValueError('unvalidated release')
    backup=bundle/'rollback';backup.mkdir()
    shutil.copyfile(client/'main.py',backup/'main.py');shutil.copyfile(rt/'config/paper_observation_five.local.json',backup/'profile.private.json')
    shutil.copytree(rt/'state/paper_observation_five',backup/'state')
    cp={str(p.relative_to(rt)):prior.sha(p) for p in (rt/'state/paper_observation_five/products').glob('*/core_checkpoint.json')}
    if len(cp)!=5:raise ValueError('five checkpoint books required')
    shutil.copyfile(stage/'main.py',client/'main.py');shutil.copyfile(stage/'_strategy_runtime/config/paper_observation_five.local.json',rt/'config/paper_observation_five.local.json')
    for pin in (rt/'state/paper_observation_five/products').glob('*/contract_pin.json'):
        data=prior.read(pin);data['config_sha256']=plan['new_config_sha256'];atomic(pin,data)
    state.update(config_sha256=plan['new_config_sha256'],pause_new=True)
    atomic(rt/'state/paper_observation_five/execution.json',state)
    subprocess.run([sys.executable,'-X','utf8',str(client/'main.py'),'--self-check','--check-config'],check=True)
    if any(prior.sha(rt/k)!=v for k,v in cp.items()):raise RuntimeError('checkpoint bytes changed unexpectedly')
    atomic(bundle/'installed.local.json',dict(at=datetime.now(timezone.utc).isoformat(),main_sha256_lf=plan['new_main_sha256_lf'],config_sha256=plan['new_config_sha256'],checkpoint_hashes=cp,pause_new=True,execution_policy='PAPER_EXEC_V5',entry_policy=plan.get('entry_policy_version'),orders_sent=0))
    print(json.dumps(dict(installed=True,checkpoints_preserved=5,pause_new=True)))


def rollback(client,bundle):
    """Flat-only code/profile rollback, retaining current fills and checkpoints."""
    rt,ev,state,status=prior.flat_stopped(client)
    plan=prior.read(bundle/'release_plan.local.json');backup=bundle/'rollback'
    if Path(plan['active_client']).resolve()!=client.resolve():raise ValueError('wrong client')
    if prior.lfsha(client/'main.py')!=plan['new_main_sha256_lf']:raise ValueError('unexpected current release')
    if prior.lfsha(backup/'main.py')!=plan['old_main_sha256_lf'] or prior.sha(backup/'profile.private.json')!=plan['old_config_sha256']:raise ValueError('rollback bytes changed')
    retained={str(p.relative_to(rt)):prior.sha(p) for p in (rt/'state/paper_observation_five/products').glob('*/core_checkpoint.json')}
    archive=bundle/'before_rollback';archive.mkdir()
    shutil.copyfile(client/'main.py',archive/'main.py')
    shutil.copyfile(rt/'config/paper_observation_five.local.json',archive/'profile.private.json')
    shutil.copyfile(rt/'state/paper_observation_five/execution.json',archive/'execution.private.json')
    shutil.copyfile(backup/'main.py',client/'main.py')
    shutil.copyfile(backup/'profile.private.json',rt/'config/paper_observation_five.local.json')
    for pin in (rt/'state/paper_observation_five/products').glob('*/contract_pin.json'):
        data=prior.read(pin);data['config_sha256']=plan['old_config_sha256'];atomic(pin,data)
    state.update(config_sha256=plan['old_config_sha256'],pause_new=True)
    atomic(rt/'state/paper_observation_five/execution.json',state)
    subprocess.run([sys.executable,'-X','utf8',str(client/'main.py'),'--self-check','--check-config'],check=True)
    if any(prior.sha(rt/k)!=v for k,v in retained.items()):raise RuntimeError('rollback rewound checkpoints')
    atomic(bundle/'rollback.local.json',dict(at=datetime.now(timezone.utc).isoformat(),main_sha256_lf=plan['old_main_sha256_lf'],execution_history_retained=True,checkpoint_hashes=retained,pause_new=True,orders_sent=0))
    print(json.dumps(dict(rolled_back=True,pause_new=True,execution_history_retained=True)))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','install','rollback']);p.add_argument('--client',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True);p.add_argument('--entry',action='store_true');a=p.parse_args()
    if a.mode=='prepare':prepare(a.client,a.bundle,a.entry)
    else:dict(install=install,rollback=rollback)[a.mode](a.client,a.bundle)
