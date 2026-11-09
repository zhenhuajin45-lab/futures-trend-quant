"""Stage/install an execution-only patch. Preserve config, ledger and pauses.

User controls all client start/stop actions. This tool never clears protection,
starts/stops a process or calls an order API. A running owned process blocks it.
"""
from pathlib import Path
from datetime import datetime,timezone
import argparse,json,os,shutil,subprocess,sys,tempfile
from tools import paper_core_v2_release as prior
from tools.build_standalone_paper import build
from futures_trend_quant.live.paper_broker import atomic
from futures_trend_quant.live.paper_config import frozen_core_digest

VERSION='PAPER_EXECUTION_REPAIR_R6_20261110'


def inventory():
    cmd='Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CreationDate | ConvertTo-Json -Compress'
    raw=subprocess.check_output(['powershell','-NoProfile','-Command',cmd],text=True,encoding='utf8')
    value=json.loads(raw);return value if isinstance(value,list) else [value]


def owned_processes(host_pid,rows):
    ids={host_pid};changed=True
    while changed:
        before=len(ids);ids.update(x['ProcessId'] for x in rows if x['ParentProcessId'] in ids);changed=len(ids)!=before
    return [x for x in rows if x['ProcessId'] in ids]


def stopped(plan):
    current={x['ProcessId']:x for x in inventory()}
    alive=[x['ProcessId'] for x in plan['owned_at_prepare'] if x['ProcessId'] in current
        and x['CreationDate']==current[x['ProcessId']]['CreationDate']]
    if alive:raise ValueError('USER_STOP_REQUIRED_OWNED_PROCESSES_STILL_RUNNING:'+str(alive))


def self_check(stage,python=None):
    env=dict(os.environ);env.pop('PYTHONPATH',None)
    result=subprocess.run([str(python or sys.executable),'-X','utf8',str(stage/'main.py'),'--self-check','--check-config'],
        env=env,capture_output=True,text=True,encoding='utf8',check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


def replace_main(source,target):
    fd,temporary=tempfile.mkstemp(dir=target.parent,prefix='.reviewed_main_',suffix='.tmp')
    try:
        with os.fdopen(fd,'wb') as f:f.write(source.read_bytes());f.flush();os.fsync(f.fileno())
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def prepare(client,bundle,sdk_source=None,host_python=None):
    bundle.mkdir(parents=True,exist_ok=False);stage=bundle/'client';rt=stage/'_strategy_runtime';active=client/'_strategy_runtime'
    (rt/'config').mkdir(parents=True)
    (stage/'ops').mkdir();shutil.copyfile(prior.ROOT/'ops/night_inspect.py',stage/'ops/night_inspect.py')
    config=prior.read(active/'config/paper_observation_five.local.json')
    for source in (active/'config').glob('*.json'):shutil.copyfile(source,rt/'config'/source.name)
    if config.get('warmup_no_trade_manifest'):
        target=rt/config['warmup_no_trade_manifest'];target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(active/config['warmup_no_trade_manifest'],target)
    sdk_files={}
    if sdk_source:
        if not (sdk_source/'gm/csdk/c_sdk.cp313-win_amd64.pyd').is_file():raise ValueError('reviewed CP313 SDK required')
        if not (sdk_source/'gm-3.0.186.dist-info/METADATA').is_file():raise ValueError('SDK version identity required')
        shutil.copytree(sdk_source/'gm',rt/'gm',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        shutil.copytree(sdk_source/'gm-3.0.186.dist-info',rt/'gm-3.0.186.dist-info')
        sdk_files={str(p.relative_to(rt)):prior.sha(p) for folder in ('gm','gm-3.0.186.dist-info')
            for p in (rt/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    receipt=build(prior.ROOT,stage/'main.py');check=self_check(stage,host_python)
    if prior.sha(rt/'config/paper_observation_five.local.json')!=prior.sha(active/'config/paper_observation_five.local.json'):
        raise ValueError('execution-only release may not change profile bytes')
    if frozen_core_digest(prior.ROOT)!=config['core_policy_sha256_lf']:raise ValueError('core changed')
    host=prior.read(active/'evidence/paper_observation_five/gm_platform_host.local.json')
    plan=dict(version=VERSION,prepared_at=datetime.now(timezone.utc).isoformat(),active_client=str(client),
        old_main_sha256_lf=prior.lfsha(client/'main.py'),new_main_sha256_lf=prior.lfsha(stage/'main.py'),
        config_sha256=prior.sha(active/'config/paper_observation_five.local.json'),core_sha256=frozen_core_digest(prior.ROOT),
        self_check=check,owned_at_prepare=owned_processes(host['pid'],inventory()),
        requires_user_stop=True,pause_restore_authorized=False,orders_sent=0,
        host_python=str(host_python or sys.executable),private_sdk_files=sdk_files,
        private_sdk_version='3.0.186' if sdk_files else None,global_python_environment_modified=False)
    plan['old_inspection_sha256']=prior.sha(client/'ops/night_inspect.py')
    plan['new_inspection_sha256']=prior.sha(stage/'ops/night_inspect.py')
    atomic(bundle/'release_plan.local.json',plan)
    print(json.dumps(dict(prepared=True,main_sha256_lf=plan['new_main_sha256_lf'],active_modified=False,pause_cleared=False)))


def install(client,bundle):
    plan=prior.read(bundle/'release_plan.local.json');gate=prior.read(bundle/'validation_gate.local.json')
    if Path(plan['active_client']).resolve()!=client.resolve():raise ValueError('wrong client')
    if gate.get('main_sha256_lf')!=plan['new_main_sha256_lf'] or not all(gate.get(k) is True for k in
            ('tests_passed','real_sdk_tick_passed','async_native_chain_passed','core_unchanged','production_review_passed')):
        raise ValueError('release validation incomplete')
    stopped(plan);rt,ev,state,status=prior.flat_stopped(client)
    profile=rt/'config/paper_observation_five.local.json';stage=bundle/'client'
    if prior.sha(profile)!=plan['config_sha256'] or prior.lfsha(client/'main.py')!=plan['old_main_sha256_lf']:
        raise ValueError('active release changed since staging')
    if prior.lfsha(stage/'main.py')!=plan['new_main_sha256_lf']:raise ValueError('staged main changed')
    if plan.get('new_inspection_sha256') and (prior.sha(stage/'ops/night_inspect.py')!=plan['new_inspection_sha256']
            or prior.sha(client/'ops/night_inspect.py')!=plan['old_inspection_sha256']):raise ValueError('inspection script changed')
    retained={str(p.relative_to(rt)):prior.sha(p) for p in (rt/'state/paper_observation_five').rglob('*') if p.is_file()}
    for folder in ('gm','gm-3.0.186.dist-info'):
        existing={str(p.relative_to(rt)) for p in (rt/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
        if not existing.issubset(plan.get('private_sdk_files',{})):raise ValueError('unreviewed private SDK files present')
    rollback=bundle/'rollback';rollback.mkdir();shutil.copyfile(client/'main.py',rollback/'main.py');shutil.copyfile(profile,rollback/'profile.private.json')
    if plan.get('new_inspection_sha256'):shutil.copyfile(client/'ops/night_inspect.py',rollback/'night_inspect.py')
    for rel,sha in plan.get('private_sdk_files',{}).items():
        if prior.sha(stage/'_strategy_runtime'/rel)!=sha:raise ValueError('staged SDK changed')
        if (rt/rel).exists() and prior.sha(rt/rel)!=sha:raise ValueError('unreviewed private SDK already present')
    for rel in plan.get('private_sdk_files',{}):
        dest=rt/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(stage/'_strategy_runtime'/rel,dest)
    if any(prior.sha(rt/rel)!=sha for rel,sha in plan.get('private_sdk_files',{}).items()):
        raise RuntimeError('installed private SDK hash mismatch')
    replace_main(stage/'main.py',client/'main.py');check=self_check(client,plan['host_python'])
    if prior.lfsha(client/'main.py')!=plan['new_main_sha256_lf']:raise RuntimeError('installed main hash mismatch')
    if plan.get('new_inspection_sha256'):replace_main(stage/'ops/night_inspect.py',client/'ops/night_inspect.py')
    if prior.sha(profile)!=plan['config_sha256'] or any(prior.sha(rt/p)!=sha for p,sha in retained.items()):
        raise RuntimeError('protected state/profile bytes changed')
    atomic(bundle/'installed.local.json',dict(version=VERSION,at=datetime.now(timezone.utc).isoformat(),
        main_sha256_lf=prior.lfsha(client/'main.py'),config_sha256=plan['config_sha256'],state_hashes=retained,
        pause_retained=state['pause_new'],pause_metadata=state.get('pause_metadata'),orders_sent=0,
        user_start_required=True,actual_host_loaded=False,self_check=check,
        private_sdk_version=plan.get('private_sdk_version'),global_python_environment_modified=False))
    print(json.dumps(dict(installed=True,state_files_preserved=len(retained),pause_retained=state['pause_new'],actual_host_loaded=False)))


def rollback(client,bundle):
    plan=prior.read(bundle/'release_plan.local.json')
    if Path(plan['active_client']).resolve()!=client.resolve():raise ValueError('wrong client')
    # Includes the original host's descendants. A newer running host is also
    # rejected by flat_stopped, even if the prepared PID has since exited.
    stopped(plan);rt,ev,state,status=prior.flat_stopped(client)
    if (prior.lfsha(client/'main.py')!=plan['new_main_sha256_lf']
            or prior.lfsha(bundle/'rollback/main.py')!=plan['old_main_sha256_lf']
            or prior.sha(rt/'config/paper_observation_five.local.json')!=plan['config_sha256']):
        raise ValueError('rollback identity mismatch')
    retained={str(p.relative_to(rt)):prior.sha(p) for p in (rt/'state/paper_observation_five').rglob('*') if p.is_file()}
    replace_main(bundle/'rollback/main.py',client/'main.py');self_check(client,plan['host_python'])
    if plan.get('new_inspection_sha256'):replace_main(bundle/'rollback/night_inspect.py',client/'ops/night_inspect.py')
    if any(prior.sha(rt/p)!=sha for p,sha in retained.items()):raise RuntimeError('rollback changed ledger/state')
    atomic(bundle/'rollback.local.json',dict(at=datetime.now(timezone.utc).isoformat(),main_sha256_lf=plan['old_main_sha256_lf'],
        state_hashes=retained,pause_retained=state['pause_new'],actual_host_loaded=False,orders_sent=0,
        private_sdk_recovery_retained=bool(plan.get('private_sdk_files'))))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','install','rollback']);p.add_argument('--client',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True);p.add_argument('--sdk-source',type=Path);p.add_argument('--host-python',type=Path);a=p.parse_args()
    if a.mode=='prepare':prepare(a.client.resolve(),a.bundle.resolve(),a.sdk_source,a.host_python)
    else:{'install':install,'rollback':rollback}[a.mode](a.client.resolve(),a.bundle.resolve())
