"""R8 state-preserving release. User exclusively owns process control."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import ast
import json
import shutil
import sys
from tools import paper_execution_r6_release as base
from tools.build_standalone_paper import build
from futures_trend_quant.live.paper_config import frozen_core_digest
from futures_trend_quant.live.paper_broker import atomic

VERSION='GOLD_DIAGNOSTICS_AND_STRUCTURE_SHADOW_R8_20261111'


def validate_active_sources(client):
    tree=ast.parse((client/'main.py').read_text(encoding='utf-8-sig'))
    hashes=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
        and any(isinstance(t,ast.Name) and t.id=='SOURCE_SHA256' for t in n.targets))
    rt=client/'_strategy_runtime'
    for rel,sha in hashes.items():
        p=(rt/rel).resolve()
        if not p.is_relative_to(rt.resolve()) or base.prior.lfsha(p)!=sha:
            raise ValueError('active runtime source changed; preserve operator edits')


def prepare_rollback(client,bundle):
    """Read-only fresh rollback scope. It grants no stop/start or recovery."""
    p=bundle/'release_plan.local.json';plan=base.prior.read(p)
    if Path(plan['active_client']).resolve()!=client.resolve():raise ValueError('rollback client mismatch')
    if base.prior.lfsha(client/'main.py')!=plan['new_main_sha256_lf'] or base.prior.sha(client/'_strategy_runtime/config/paper_observation_five.local.json')!=plan['new_config_sha256']:raise ValueError('rollback active identity changed')
    host=base.prior.read(client/'_strategy_runtime/evidence/paper_observation_five/gm_platform_host.local.json')
    plan['rollback_handoff']=dict(host_pid_at_prepare=host['pid'],owned_at_prepare=base.owned_processes(host['pid'],base.inventory()),at=datetime.now(timezone.utc).isoformat())
    atomic(p,plan)


def prepare(client,bundle):
    bundle.mkdir(parents=True,exist_ok=False)
    active=client/'_strategy_runtime';stage=bundle/'client';rt=stage/'_strategy_runtime'
    (rt/'config').mkdir(parents=True)
    for p in (active/'config').glob('*.json'):shutil.copyfile(p,rt/'config'/p.name)
    for name in ('gm','gm-3.0.186.dist-info'):
        shutil.copytree(active/name,rt/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    c=base.prior.read(active/'config/paper_observation_five.local.json')
    if c.get('entry_policy_version')!='PAPER_ENTRY_V5':raise ValueError('R8 requires reviewed V5 profile')
    if c.get('warmup_no_trade_manifest'):
        target=rt/c['warmup_no_trade_manifest'];target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(active/c['warmup_no_trade_manifest'],target)
    c.update(permission_policy='CLOSED_FACTS_PERMISSION_V2_20261111',core_policy_sha256_lf=frozen_core_digest(base.prior.ROOT),
             divergence_structure_policy='DIVERGENCE_STRUCTURE_REPLACEMENT_SHADOW_V1_20261111',divergence_structure_mode='SHADOW')
    atomic(rt/'config/paper_observation_five.local.json',c)
    receipt=build(base.prior.ROOT,stage/'main.py');check=base.self_check(stage)
    host=base.prior.read(active/'evidence/paper_observation_five/gm_platform_host.local.json')
    # A concrete rollback source exists before asking for user stop. Private only.
    backup=bundle/'rollback';backup.mkdir();shutil.copyfile(client/'main.py',backup/'main.py')
    shutil.copyfile(active/'config/paper_observation_five.local.json',backup/'profile.private.json')
    plan=dict(version=VERSION,prepared_at=datetime.now(timezone.utc).isoformat(),active_client=str(client),
        old_main_sha256_lf=base.prior.lfsha(client/'main.py'),old_config_sha256=base.prior.sha(active/'config/paper_observation_five.local.json'),
        new_main_sha256_lf=base.prior.lfsha(stage/'main.py'),new_config_sha256=base.prior.sha(rt/'config/paper_observation_five.local.json'),
        new_core_sha256_lf=c['core_policy_sha256_lf'],embedded_files=receipt['embedded_files'],self_check=check,
        host_pid_at_prepare=host['pid'],owned_at_prepare=base.owned_processes(host['pid'],base.inventory()),host_python=sys.executable,
        installed=False,actual_host_loaded=False,process_control='USER_ONLY',pause_cleared=False,
        checkpoints='PRESERVE_EXACT_BYTES;LAZY_ADDITIVE_SHADOW_STATE',enforcement='BLOCKED_NOT_ADMITTED',orders_sent=0)
    atomic(bundle/'release_plan.local.json',plan)
    return plan


def install(client,bundle,rollback=False):
    plan=base.prior.read(bundle/'release_plan.local.json')
    if plan['version']!=VERSION or Path(plan['active_client']).resolve()!=client.resolve():raise ValueError('release identity')
    gate=base.prior.read(bundle/'validation_gate.local.json')
    if not all(gate.get(k) is True for k in ('tests_passed','production_review_passed','native_py310_passed','host_py313_passed','historical_audit_completed')):raise ValueError('release validation incomplete')
    if gate.get('main_sha256_lf')!=plan['new_main_sha256_lf'] or gate.get('core_sha256_lf')!=plan['new_core_sha256_lf'] or gate.get('config_sha256')!=plan['new_config_sha256']:raise ValueError('validation fingerprint mismatch')
    guard_plan=plan
    if rollback:
        if not plan.get('rollback_handoff'):raise ValueError('fresh explicit rollback preparation required')
        guard_plan=dict(plan,**plan['rollback_handoff'])
    base.stopped(guard_plan)
    rt,ev,state,status=base.prior.flat_stopped(client)
    current_host=base.prior.read(ev/'gm_platform_host.local.json')
    if current_host['pid']!=guard_plan['host_pid_at_prepare']:raise ValueError('fresh release or rollback preparation required for changed host')
    old='new' if rollback else 'old';new='old' if rollback else 'new'
    if base.prior.lfsha(client/'main.py')!=plan[old+'_main_sha256_lf'] or base.prior.sha(rt/'config/paper_observation_five.local.json')!=plan[old+'_config_sha256']:raise ValueError('active source/profile changed')
    source=bundle/'rollback' if rollback else bundle/'client'
    main=source/'main.py';profile=source/'profile.private.json' if rollback else source/'_strategy_runtime/config/paper_observation_five.local.json'
    if base.prior.lfsha(main)!=plan[new+'_main_sha256_lf'] or base.prior.sha(profile)!=plan[new+'_config_sha256']:raise ValueError('staged source/profile changed')
    validate_active_sources(client)
    pa=rt/'state/paper_observation_five/price_action_shadow/observer.json';pa_migration=None
    if rollback and pa.exists():
        previous=base.prior.read(pa)
        if previous.get('policy')=='PRICE_ACTION_SHADOW_V2_WINDOW_COVERAGE_20261111':
            from futures_trend_quant.observation.price_action.models import digest,DEFAULTS
            if previous.get('parameters_sha256')!=digest(DEFAULTS):raise ValueError('PA rollback parameters changed')
            pa_migration=dict(previous,policy='PRICE_ACTION_SHADOW_V1_20261110')
            pa_migration['migrations']=list(previous.get('migrations',[]))+[dict(kind='EXPLICIT_ROLLBACK_POLICY_COMPATIBILITY',source_state_sha256=base.prior.sha(pa),at=datetime.now(timezone.utc).isoformat(),known_at_and_events_retained=True)]
    protected={str(p.relative_to(rt)):base.prior.sha(p) for p in (rt/'state/paper_observation_five').rglob('*') if p.is_file() and p.name not in ('execution.json','contract_pin.json') and not (pa_migration and p==pa)}
    before=json.loads(json.dumps(state));pins=list((rt/'state/paper_observation_five/products').glob('*/contract_pin.json'))
    if len(pins)!=5:raise ValueError('five contract pins required')
    # Fail-closed handoff: no process/control permission is created by this tool.
    base.stopped(guard_plan);base.prior.flat_stopped(client)
    if not rollback:shutil.copytree(rt/'state/paper_observation_five',bundle/'rollback/state_at_install')
    base.replace_main(main,client/'main.py');base.replace_main(profile,rt/'config/paper_observation_five.local.json')
    for p in pins:
        pin=base.prior.read(p);pin['config_sha256']=plan[new+'_config_sha256'];atomic(p,pin)
    state['config_sha256']=plan[new+'_config_sha256'];atomic(rt/'state/paper_observation_five/execution.json',state)
    if pa_migration:
        shutil.copyfile(pa,bundle/'rollback/pa_before_explicit_rollback.private.json');atomic(pa,pa_migration)
    if any(base.prior.sha(rt/rel)!=sha for rel,sha in protected.items()):raise RuntimeError('protected state changed; manual review required')
    if {k:v for k,v in state.items() if k!='config_sha256'}!={k:v for k,v in before.items() if k!='config_sha256'}:raise RuntimeError('ledger/protection changed')
    atomic(bundle/('rollback.local.json' if rollback else 'installed.local.json'),dict(version=VERSION,at=datetime.now(timezone.utc).isoformat(),
        main_sha256_lf=plan[new+'_main_sha256_lf'],config_sha256=plan[new+'_config_sha256'],protected_state_files=len(protected),
        pause_retained=state['pause_new'],halt_reason_retained=True,actual_host_loaded=False,user_start_required=True,orders_sent=0))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','prepare-rollback','install','rollback']);p.add_argument('--client',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True);a=p.parse_args()
    if a.mode=='prepare':print(json.dumps(prepare(a.client.resolve(),a.bundle.resolve())))
    elif a.mode=='prepare-rollback':prepare_rollback(a.client.resolve(),a.bundle.resolve())
    else:install(a.client.resolve(),a.bundle.resolve(),a.mode=='rollback')
