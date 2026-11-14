"""R15 stopped-host release. No start/stop, recovery, order or automatic rollback."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,ast,json,shutil,sys,hashlib
from tools import paper_gold_r8_release as r8
from tools.build_standalone_paper import build
from futures_trend_quant.live.paper_broker import atomic
from futures_trend_quant.live.paper_symbol_health import POLICY
from futures_trend_quant.live.paper_clock_health import SAMPLER_POLICY,STARTUP_POLICY
from futures_trend_quant.live.paper_tq_market import INPUT_WAIT_POLICY
from futures_trend_quant.live.paper_products import TEN_POOL
from futures_trend_quant.live.paper_config import frozen_core_digest

VERSION='GM_ENTRY_CHAIN_R15_20261114'
PROFILE=Path('config/paper_observation_five.local.json')


from tools.paper_gm_r13_release import stopped


def source_hashes(main):
    tree=ast.parse(main.read_text(encoding='utf-8-sig'))
    return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
        and any(isinstance(t,ast.Name) and t.id=='SOURCE_SHA256' for t in n.targets))


def prepare(client,bundle):
    r8.validate_active_sources(client)
    active=client/'_strategy_runtime';old=r8.base.prior.read(active/PROFILE)
    if old['universe']!=list(TEN_POOL) or old['entry_policy_version']!='PAPER_ENTRY_V5':raise ValueError('R15_REQUIRES_REVIEWED_TEN_V5')
    if (old.get('gm_consumed_input_policy')!='GM_CONSUMED_INPUT_CACHE_V1_20261112'
            or old.get('gm_handoff_policy')!='GM_PRIORITY_HANDOFF_V1_20261112'):
        raise ValueError('R15_REQUIRES_GM_CONSUMED_INPUT_PROFILE')
    if frozen_core_digest(r8.base.prior.ROOT)!=old['core_policy_sha256_lf']:raise ValueError('R15_CORE_MUST_REMAIN_UNCHANGED')
    bundle.mkdir(parents=True,exist_ok=False);stage=bundle/'client';rt=stage/'_strategy_runtime'
    (rt/'config').mkdir(parents=True)
    for p in (active/'config').glob('*.json'):shutil.copyfile(p,rt/'config'/p.name)
    for name in ('gm','gm-3.0.186.dist-info'):
        shutil.copytree(active/name,rt/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    inputs={}
    def private(relative):
        src=(active/relative).resolve();dst=(rt/relative).resolve()
        if not src.is_relative_to(active.resolve()/'data/cache') or not dst.is_relative_to(rt.resolve()/'data/cache'):raise ValueError('R15_PRIVATE_PATH')
        dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst);inputs[str(relative)]=r8.base.prior.sha(src)
    if old.get('warmup_no_trade_manifest'):private(old['warmup_no_trade_manifest'])
    feed=old['tq_feed'];private(feed['seed_manifest']);private(feed['auth_file'])
    for row in r8.base.prior.read(active/feed['seed_manifest'])['products'].values():
        private(row['seed']);private(row['metadata'])
    new=dict(old,symbol_market_policy=POLICY,clock_sampler_policy=SAMPLER_POLICY,clock_startup_policy=STARTUP_POLICY,tq_input_wait_policy=INPUT_WAIT_POLICY,notice_evaluation_policy='NOTICE_CAUSAL_PREFILTER_V1_20261112',clock_probe_policy='CLOCK_PARALLEL_BOUNDED_V1_20261114',core_aggregation_policy='CORE_COMPLETE_BUCKET_CACHE_V1_20261114');atomic(rt/PROFILE,new)
    receipt=build(r8.base.prior.ROOT,stage/'main.py');check=r8.base.self_check(stage)
    backup=bundle/'rollback';backup.mkdir();shutil.copyfile(client/'main.py',backup/'main.py')
    (stage/'ops').mkdir();shutil.copyfile(r8.base.prior.ROOT/'ops/night_inspect.py',stage/'ops/night_inspect.py')
    shutil.copyfile(client/'ops/night_inspect.py',backup/'night_inspect.py')
    shutil.copyfile(active/PROFILE,backup/'profile.private.json')
    hashes=source_hashes(client/'main.py')
    for rel in hashes:
        dst=backup/'runtime'/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(active/rel,dst)
    host=r8.base.prior.read(active/'evidence/paper_observation_five/gm_platform_host.local.json')
    owned={p['ProcessId']:p for p in r8.base.owned_processes(host['pid'],r8.base.inventory())}
    plan=dict(version=VERSION,prepared_at=datetime.now(timezone.utc).isoformat(),active_client=str(client),
        host_pid_at_prepare=host['pid'],owned_at_prepare=list(owned.values()),host_python=sys.executable,
        old_main_sha256_lf=r8.base.prior.lfsha(client/'main.py'),old_config_sha256=r8.base.prior.sha(active/PROFILE),
        new_main_sha256_lf=r8.base.prior.lfsha(stage/'main.py'),new_config_sha256=r8.base.prior.sha(rt/PROFILE),
        new_core_sha256_lf=new['core_policy_sha256_lf'],embedded_files=receipt['embedded_files'],self_check=check,
        old_inspection_sha256=r8.base.prior.sha(client/'ops/night_inspect.py'),new_inspection_sha256=r8.base.prior.sha(stage/'ops/night_inspect.py'),
        private_inputs=inputs,installed=False,actual_host_loaded=False,user_start_required=True,
        clock_policy_version=old.get('clock_policy_version'),clock_source_policy_version='ALIYUN_TENCENT_PUBLIC_NTP_V1_20261109',
        quote_policy_version=old.get('quote_policy_version'),runtime_health_version='R15',
        state_migration='CONFIG_IDENTITY_ONLY_ALL_PROTECTION_AND_LEDGER_STATE_PRESERVED',
        policies=dict(shutdown='HOST_SHUTDOWN_FACTS_V2_20261112',gm_consumed_input='GM_CONSUMED_INPUT_CACHE_V1_20261112',gm_handoff='GM_PRIORITY_HANDOFF_V1_20261112',gm_notice_input='GM_NOTICE_COMMITTED_INPUT_V1_20261113',clock_probe='CLOCK_PARALLEL_BOUNDED_V1_20261114',core_aggregation='CORE_COMPLETE_BUCKET_CACHE_V1_20261114',quote_handoff_evidence='QUOTE_HANDOFF_EVIDENCE_V1_20261113',ntp_raw_identity='NTP_RAW_SAMPLE_IDENTITY_V1_20261113',clock_startup=STARTUP_POLICY,tq_input_wait=INPUT_WAIT_POLICY,notice_evaluation='NOTICE_CAUSAL_PREFILTER_V1_20261112',clock_sampler=SAMPLER_POLICY,symbol_market=POLICY,gm_closed_m1=old.get('gm_closed_m1_policy'),entry='PAPER_ENTRY_V5',execution='PAPER_EXEC_V5'),orders_sent=0)
    atomic(bundle/'release_plan.local.json',plan);return plan


def install(client,bundle,refresh_handoff=None):
    plan=r8.base.prior.read(bundle/'release_plan.local.json');gate=r8.base.prior.read(bundle/'validation_gate.local.json')
    if plan['version']!=VERSION or Path(plan['active_client']).resolve()!=client.resolve():raise ValueError('R15_RELEASE_IDENTITY')
    required=('tests_passed','production_review_passed','native_py310_passed','host_py313_passed','historical_audit_completed','rollback_prepared')
    if not all(gate.get(k) is True for k in required):raise ValueError('R15_VALIDATION_INCOMPLETE')
    for key in ('main_sha256_lf','config_sha256','core_sha256_lf'):
        if gate.get(key)!=plan['new_'+key]:raise ValueError('R15_VALIDATION_FINGERPRINT')
    if plan.get('new_inspection_sha256') and gate.get('inspection_sha256')!=plan['new_inspection_sha256']:raise ValueError('R15_INSPECTION_GATE')
    stopped(plan);rt,ev,state,status=r8.base.prior.flat_stopped(client)
    if r8.base.prior.read(ev/'gm_platform_host.local.json')['pid']!=plan['host_pid_at_prepare']:raise ValueError('R15_CHANGED_HOST')
    if r8.base.prior.lfsha(client/'main.py')!=plan['old_main_sha256_lf'] or r8.base.prior.sha(rt/PROFILE)!=plan['old_config_sha256']:raise ValueError('R15_ACTIVE_CHANGED')
    r8.validate_active_sources(client)
    stage=bundle/'client';staged=stage/'_strategy_runtime'
    if r8.base.prior.lfsha(stage/'main.py')!=plan['new_main_sha256_lf'] or r8.base.prior.sha(staged/PROFILE)!=plan['new_config_sha256']:raise ValueError('R15_STAGE_CHANGED')
    if plan.get('new_inspection_sha256') and (r8.base.prior.sha(stage/'ops/night_inspect.py')!=plan['new_inspection_sha256'] or r8.base.prior.sha(client/'ops/night_inspect.py')!=plan['old_inspection_sha256']):raise ValueError('R15_INSPECTION_CHANGED')
    for rel,h in plan['private_inputs'].items():
        if r8.base.prior.sha(rt/rel)!=h or r8.base.prior.sha(staged/rel)!=h:raise ValueError('R15_PRIVATE_INPUT_CHANGED')
    old=r8.base.prior.read(rt/PROFILE);new=r8.base.prior.read(staged/PROFILE)
    if new!=dict(old,symbol_market_policy=POLICY,clock_sampler_policy=SAMPLER_POLICY,clock_startup_policy=STARTUP_POLICY,tq_input_wait_policy=INPUT_WAIT_POLICY,notice_evaluation_policy='NOTICE_CAUSAL_PREFILTER_V1_20261112',clock_probe_policy='CLOCK_PARALLEL_BOUNDED_V1_20261114',core_aggregation_policy='CORE_COMPLETE_BUCKET_CACHE_V1_20261114'):raise ValueError('R15_PROFILE_SCOPE')
    pins=list((rt/'state/paper_observation_five/products').glob('*/contract_pin.json'))
    if {p.parent.name for p in pins}!={cs.replace('.','_') for cs in TEN_POOL}:raise ValueError('R15_TEN_PINS_REQUIRED')
    pin_values={p:r8.base.prior.read(p) for p in pins}
    if any(p['config_sha256']!=plan['old_config_sha256'] for p in pin_values.values()):raise ValueError('R15_PIN_IDENTITY')
    changed={p.relative_to(rt).as_posix() for p in pins}|{'state/paper_observation_five/execution.json'}
    # All ledgers, candidates, checkpoints, observer state, proofs and raw
    # evidence are protected, not just a selected set of current checkpoints.
    protected={p.relative_to(rt).as_posix():r8.base.prior.sha(p) for folder in ('state/paper_observation_five','evidence/paper_observation_five')
        for p in (rt/folder).rglob('*') if p.is_file() and p.relative_to(rt).as_posix() not in changed}
    protected_stats={rel:((rt/rel).stat().st_size,(rt/rel).stat().st_mtime_ns) for rel in protected}
    if refresh_handoff is not None:
        # A large evidence archive may consume the original account lifetime.
        # Re-read only after hashing, preserving every pre-read log prefix and
        # archiving the overwritten snapshot. Never extend its 30-second gate.
        proof='evidence/paper_observation_five/controlled_handoff_snapshot.local.json'
        logs={'evidence/paper_observation_five/native_stages_reader.local.jsonl',
              'evidence/paper_observation_five/readonly_handoff_sdk.local.log'}
        archive=bundle/('rollback/final_handoff_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'));archive.mkdir(parents=True)
        for rel in {proof}|logs:
            if (rt/rel).exists():shutil.copyfile(rt/rel,archive/Path(rel).name)
        stopped(plan);refresh_handoff()
        for rel,h in list(protected.items()):
            p=rt/rel;current=(p.stat().st_size,p.stat().st_mtime_ns)
            if current==protected_stats[rel]:continue
            if rel in logs:
                with p.open('rb') as f:prefix=f.read(protected_stats[rel][0])
                if hashlib.sha256(prefix).hexdigest()!=h:raise ValueError('R15_FINAL_HANDOFF_LOG_PREFIX_CHANGED')
            elif rel!=proof:
                if r8.base.prior.sha(p)!=h:raise ValueError('R15_FINAL_HANDOFF_UNEXPECTED_MUTATION')
                continue
            protected[rel]=r8.base.prior.sha(p)
        for rel in {proof}|logs:
            if (rt/rel).exists():protected[rel]=r8.base.prior.sha(rt/rel)
    before=json.loads(json.dumps(state))
    stopped(plan)
    if r8.base.prior.lfsha(client/'main.py')!=plan['old_main_sha256_lf'] or r8.base.prior.sha(rt/PROFILE)!=plan['old_config_sha256']:raise ValueError('R15_HANDOFF_SOURCE_CHANGED')
    _,_,final_state,_=r8.base.prior.flat_stopped(client)
    if (final_state!=state or r8.base.prior.read(rt/'state/paper_observation_five/execution.json')!=state
            or any(r8.base.prior.read(p)!=pin for p,pin in pin_values.items())):raise ValueError('R15_HANDOFF_STATE_CHANGED')
    if r8.base.prior.read(ev/'gm_platform_host.local.json')['pid']!=plan['host_pid_at_prepare']:raise ValueError('R15_HANDOFF_HOST_CHANGED')
    shutil.copytree(rt/'state/paper_observation_five',bundle/'rollback/state_at_install')
    atomic(bundle/'preinstall_protected.local.json',protected)
    atomic(bundle/'preinstall_sizes.local.json',{rel:(rt/rel).stat().st_size for rel in protected})
    r8.base.replace_main(stage/'main.py',client/'main.py');r8.base.replace_main(staged/PROFILE,rt/PROFILE)
    if plan.get('new_inspection_sha256'):r8.base.replace_main(stage/'ops/night_inspect.py',client/'ops/night_inspect.py')
    for p,pin in pin_values.items():atomic(p,dict(pin,config_sha256=plan['new_config_sha256']))
    atomic(rt/'state/paper_observation_five/execution.json',dict(state,config_sha256=plan['new_config_sha256']))
    check=r8.base.self_check(client)  # Extract only after the complete stopped-host gate.
    if any(r8.base.prior.sha(rt/rel)!=h for rel,h in protected.items()):raise RuntimeError('R15_PROTECTED_BYTES_CHANGED_MANUAL_REVIEW_NO_AUTO_ROLLBACK')
    after=r8.base.prior.read(rt/'state/paper_observation_five/execution.json')
    if after!=dict(before,config_sha256=plan['new_config_sha256']):raise RuntimeError('R15_EXECUTION_STATE_SCOPE')
    for p,pin in pin_values.items():
        if r8.base.prior.read(p)!=dict(pin,config_sha256=plan['new_config_sha256']):raise RuntimeError('R15_PIN_SCOPE')
    for rel,h in source_hashes(client/'main.py').items():
        if r8.base.prior.lfsha(rt/rel)!=h:raise RuntimeError('R15_INSTALLED_SOURCE_MISMATCH')
    receipt=dict(version=VERSION,at=datetime.now(timezone.utc).isoformat(),installed=True,
        main_sha256_lf=plan['new_main_sha256_lf'],config_sha256=plan['new_config_sha256'],core_sha256_lf=plan['new_core_sha256_lf'],
        self_check=check,protected_files=len(protected),pins=len(pins),pause_retained=after['pause_new'],
        halt_retained=after['halt_reason']==before['halt_reason'],actual_host_loaded=False,user_start_required=True,
        trading_session_acceptance='NOT_EVALUATED_USER_START_AND_NATURAL_SAMPLES_REQUIRED',orders_sent=0)
    atomic(bundle/'installed.local.json',receipt);return receipt


def resume(client,bundle,authorization=None):
    """Explicit, fresh, single-use recovery after this release was loaded.

    Installation never calls this. The stopped-host, flat account, exact loaded
    version, health-at-stop, recent-fault and authorization checks are retained.
    """
    if not authorization:raise ValueError('R15_FRESH_EXPLICIT_RESUME_AUTHORIZATION_REQUIRED')
    if r8.base.prior.read(bundle/'release_plan.local.json')['version']!=VERSION:raise ValueError('R15_RESUME_VERSION')
    from tools.paper_runtime_v6_release import resume as controlled_resume
    return controlled_resume(client,bundle,authorization=authorization)


def rollback_before_user_start(client,bundle):
    """Explicit rollback only before any R13 run; later use needs fresh review.

    Never restore an old execution ledger or checkpoint over newer trade facts.
    A changed host generation, state file or evidence prefix blocks this path.
    """
    plan=r8.base.prior.read(bundle/'release_plan.local.json')
    if plan['version']!=VERSION or Path(plan['active_client']).resolve()!=client.resolve():raise ValueError('R13_RELEASE_IDENTITY')
    stopped(plan);rt,ev,state,status=r8.base.prior.flat_stopped(client)
    if r8.base.prior.read(ev/'gm_platform_host.local.json')['pid']!=plan['host_pid_at_prepare']:raise ValueError('ROLLBACK_AFTER_NEW_HOST_REQUIRES_NEW_COMPATIBILITY_REVIEW')
    if r8.base.prior.lfsha(client/'main.py')!=plan['new_main_sha256_lf'] or r8.base.prior.sha(rt/PROFILE)!=plan['new_config_sha256']:raise ValueError('ROLLBACK_ACTIVE_IDENTITY')
    r8.validate_active_sources(client)
    before=r8.base.prior.read(bundle/'rollback/state_at_install/execution.json')
    if state!=dict(before,config_sha256=plan['new_config_sha256']):raise ValueError('ROLLBACK_NEW_EXECUTION_FACTS_REQUIRES_REVIEW')
    protected=r8.base.prior.read(bundle/'preinstall_protected.local.json')
    migration={}  # This source-only patch never rewrites a GM checkpoint.
    if any(r8.base.prior.sha(rt/rel)!=v['after_sha256'] for rel,v in migration.items()):raise ValueError('ROLLBACK_NEW_GM_INPUT_FACTS_REQUIRE_REVIEW')
    readonly_proofs={'evidence/paper_observation_five/controlled_handoff_snapshot.local.json','evidence/paper_observation_five/readonly_handoff_sdk.local.log'}
    readonly_stages='evidence/paper_observation_five/native_stages_reader.local.jsonl'
    if readonly_stages in protected:
        size=r8.base.prior.read(bundle/'preinstall_sizes.local.json')[readonly_stages]
        with (rt/readonly_stages).open('rb') as stream:prefix=stream.read(size)
        if len(prefix)!=size or hashlib.sha256(prefix).hexdigest()!=protected[readonly_stages]:raise ValueError('ROLLBACK_READONLY_LOG_PREFIX_CHANGED')
        readonly_proofs.add(readonly_stages)
    if any(r8.base.prior.sha(rt/rel)!=h for rel,h in protected.items() if rel not in readonly_proofs and rel not in migration):raise ValueError('ROLLBACK_NEW_STATE_OR_EVIDENCE_REQUIRES_REVIEW')
    state_root=rt/'state/paper_observation_five';backup=bundle/'rollback/state_at_install'
    if {p.relative_to(state_root).as_posix() for p in state_root.rglob('*') if p.is_file()}!={p.relative_to(backup).as_posix() for p in backup.rglob('*') if p.is_file()}:raise ValueError('ROLLBACK_NEW_STATE_FILES_REQUIRES_REVIEW')
    pins=list((state_root/'products').glob('*/contract_pin.json'))
    if len(pins)!=10:raise ValueError('ROLLBACK_TEN_PINS_REQUIRED')
    for p in pins:
        previous=r8.base.prior.read(backup/p.relative_to(state_root))
        if r8.base.prior.read(p)!=dict(previous,config_sha256=plan['new_config_sha256']):raise ValueError('ROLLBACK_PIN_FACTS_CHANGED')
    for rel,value in migration.items():
        if r8.base.prior.sha(backup/Path(rel).relative_to('state/paper_observation_five'))!=value['before_sha256']:raise ValueError('ROLLBACK_GM_ARCHIVE_CHANGED')
    old_main=bundle/'rollback/main.py';old_profile=bundle/'rollback/profile.private.json'
    if r8.base.prior.lfsha(old_main)!=plan['old_main_sha256_lf'] or r8.base.prior.sha(old_profile)!=plan['old_config_sha256']:raise ValueError('ROLLBACK_ARCHIVE_CHANGED')
    if plan.get('old_inspection_sha256') and (r8.base.prior.sha(bundle/'rollback/night_inspect.py')!=plan['old_inspection_sha256'] or r8.base.prior.sha(client/'ops/night_inspect.py')!=plan['new_inspection_sha256']):raise ValueError('ROLLBACK_INSPECTION_ARCHIVE_CHANGED')
    stopped(plan);r8.base.prior.flat_stopped(client)
    r8.base.replace_main(old_main,client/'main.py');r8.base.replace_main(old_profile,rt/PROFILE)
    if plan.get('old_inspection_sha256'):
        r8.base.replace_main(bundle/'rollback/night_inspect.py',client/'ops/night_inspect.py')
    for p in pins:atomic(p,dict(r8.base.prior.read(p),config_sha256=plan['old_config_sha256']))
    atomic(state_root/'execution.json',dict(state,config_sha256=plan['old_config_sha256']))
    for rel in migration:
        source=backup/Path(rel).relative_to('state/paper_observation_five')
        if r8.base.prior.sha(source)!=migration[rel]['before_sha256']:raise ValueError('ROLLBACK_GM_ARCHIVE_CHANGED')
        r8.base.replace_main(source,rt/rel)
    check=r8.base.self_check(client)
    receipt=dict(version=VERSION,rollback=True,at=datetime.now(timezone.utc).isoformat(),self_check=check,
        scope='EXPLICIT_BEFORE_ANY_NEW_HOST_OR_TRADE_FACT;NO_OLD_LEDGER_RESTORE',pause_retained=state['pause_new'],actual_host_loaded=False,orders_sent=0)
    atomic(bundle/'rollback.local.json',receipt);return receipt


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','install','rollback-before-user-start']);p.add_argument('--client',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True);a=p.parse_args()
    action={'prepare':prepare,'install':install,'rollback-before-user-start':rollback_before_user_start}[a.mode]
    result=action(a.client.resolve(),a.bundle.resolve())
    print(json.dumps({k:v for k,v in result.items() if k not in ('private_inputs','owned_at_prepare')},ensure_ascii=False))
