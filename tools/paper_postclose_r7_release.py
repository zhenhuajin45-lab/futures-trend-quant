"""R7 reviewed release wrapper; user owns all strategy start/stop operations."""
from pathlib import Path
import argparse
import json
import shutil
from tools import paper_execution_r6_release as base
from futures_trend_quant.live.paper_broker import atomic
from futures_trend_quant.observation.price_action.models import parameters,digest

VERSION='PAPER_POSTCLOSE_REPAIR_AND_PA_SHADOW_R7_20261110'
SUPERVISION_PATCH='PAPER_SUPERVISION_CLOCK_R7_1_20261110'
RELEASE_VERSIONS=(VERSION,SUPERVISION_PATCH)
CONFIG='config/price_action_shadow.local.json'


def prepare(client,bundle,sdk_source=None,host_python=None,version=VERSION):
    if version not in RELEASE_VERSIONS:raise ValueError('unreviewed release version')
    base.prepare(client,bundle,sdk_source,host_python)
    plan=base.prior.read(bundle/'release_plan.local.json')
    source=base.prior.ROOT/'config/price_action_shadow.example.json'
    raw=base.prior.read(source);parameters(raw)
    target=bundle/'client/_strategy_runtime'/CONFIG
    shutil.copyfile(source,target)
    previous=client/'_strategy_runtime'/CONFIG
    plan.update(version=version,shadow_config_sha256=base.prior.sha(target),
                shadow_parameters_sha256=digest(raw),shadow_mode='SHADOW',
                old_shadow_config_sha256=base.prior.sha(previous) if previous.exists() else None,
                self_check=base.self_check(bundle/'client',host_python))
    atomic(bundle/'release_plan.local.json',plan)
    return plan


def install(client,bundle):
    plan=base.prior.read(bundle/'release_plan.local.json');gate=base.prior.read(bundle/'validation_gate.local.json')
    if plan.get('version') not in RELEASE_VERSIONS or not all(gate.get(k) is True for k in
            ('shadow_isolation_passed','windows_io_passed','full_day_audit_completed')):
        raise ValueError('R7 validation incomplete')
    staged=bundle/'client/_strategy_runtime'/CONFIG;active=client/'_strategy_runtime'/CONFIG
    parameters(base.prior.read(staged))
    if base.prior.sha(staged)!=plan['shadow_config_sha256']:raise ValueError('shadow staged config changed')
    previous=base.prior.sha(active) if active.exists() else None
    if previous!=plan['old_shadow_config_sha256']:raise ValueError('shadow active config changed')
    # base.install verifies host/descendant termination, fresh explicit-account
    # flat handoff, all persistent states and all ordinary validation gates.
    base.install(client,bundle)
    if active.exists():shutil.copyfile(active,bundle/'rollback/price_action_shadow.private.json')
    base.replace_main(staged,active)
    if base.prior.sha(active)!=plan['shadow_config_sha256']:raise RuntimeError('shadow config install failed')
    receipt=base.prior.read(bundle/'installed.local.json')
    receipt.update(version=plan['version'],shadow_mode='SHADOW',shadow_config_sha256=plan['shadow_config_sha256'],
                   execution_config_changed=False,protection_cleared=False,
                   self_check=base.self_check(client,plan['host_python']))
    atomic(bundle/'installed.local.json',receipt)


def rollback(client,bundle):
    plan=base.prior.read(bundle/'release_plan.local.json');active=client/'_strategy_runtime'/CONFIG
    if not active.exists() or base.prior.sha(active)!=plan['shadow_config_sha256']:
        raise ValueError('shadow rollback identity mismatch')
    backup=bundle/'rollback/price_action_shadow.private.json'
    if plan['old_shadow_config_sha256'] is not None and (not backup.exists() or base.prior.sha(backup)!=plan['old_shadow_config_sha256']):
        raise ValueError('shadow rollback backup mismatch')
    base.rollback(client,bundle)
    if plan['old_shadow_config_sha256'] is None:active.unlink()
    else:base.replace_main(backup,active)
    # Shadow state is deliberately retained, separate from all trading state.
    result=base.prior.read(bundle/'rollback.local.json');result.update(version=plan['version'],shadow_state_retained=True)
    atomic(bundle/'rollback.local.json',result)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','install','rollback'])
    p.add_argument('--client',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True)
    p.add_argument('--sdk-source',type=Path);p.add_argument('--host-python',type=Path)
    p.add_argument('--release-version',choices=RELEASE_VERSIONS,default=VERSION);a=p.parse_args()
    if a.mode=='prepare':prepare(a.client.resolve(),a.bundle.resolve(),a.sdk_source,a.host_python,a.release_version)
    else:{'install':install,'rollback':rollback}[a.mode](a.client.resolve(),a.bundle.resolve())
