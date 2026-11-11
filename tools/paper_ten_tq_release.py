"""User-authorized ten-product SIM expansion; private staging, no process control."""
from pathlib import Path
from datetime import datetime,timezone
import argparse
import json
import shutil
import sys
import math
import pandas as pd
from tools import paper_gold_r8_release as r8
from tools.build_standalone_paper import build
from futures_trend_quant.live.paper_products import OILS_POOL,TQ_ADDED,TEN_POOL,TEN_POOL_VERSION
from futures_trend_quant.live.paper_tq import POLICY,sha,atomic
from futures_trend_quant.live.paper_calendar import PaperCalendar
from futures_trend_quant.live.paper_data import MarketWorker
from futures_trend_quant.live.paper_runner import warmup_coverage
from futures_trend_quant.live.paper_config import load_paper_config

VERSION='TEN_MIXED_TQ_RELEASE_V1_20261111'


def normalize_seed(csv,symbol,cutoff):
    raw=pd.read_csv(csv)
    fields={symbol+'.'+k:k for k in ('open','high','low','close','volume','close_oi')}
    if not set(fields).issubset(raw.columns) or 'datetime_nano' not in raw:raise ValueError('TQ_CSV_SCHEMA_MISMATCH')
    frame=raw.rename(columns=fields).copy()
    frame['bob']=pd.to_datetime(frame['datetime_nano'],unit='ns',utc=True)
    frame['eob']=frame['bob']+pd.Timedelta(minutes=1)
    frame=frame.loc[frame.eob<=pd.Timestamp(cutoff).floor('min')].copy()
    if frame.eob.duplicated().any() or not frame.eob.is_monotonic_increasing:raise ValueError('TQ_SEED_DUPLICATE_OR_UNORDERED')
    if frame[list(fields.values())].isna().any().any():raise ValueError('TQ_SEED_UNKNOWN_VALUE')
    if any(not frame[k].map(lambda x:math.isfinite(float(x)) and float(x)>=0).all() for k in ('volume','close_oi')):raise ValueError('TQ_SEED_VOLUME_OR_OI_UNKNOWN')
    frame['symbol']=symbol;frame['frequency']='60s';frame['position']=frame['close_oi']
    frame['amount']=0.0  # The strategy does not consume turnover; evidence records it as unavailable.
    result=frame[['symbol','frequency','bob','eob','open','high','low','close','volume','amount','position']].tail(22000).reset_index(drop=True)
    if len(result)!=22000:raise ValueError('TQ_SEED_TOO_SHORT')
    return result


def prepare(client,bundle,inputs,auth_file,tq_python):
    r8.validate_active_sources(client)
    active=client/'_strategy_runtime';old=r8.base.prior.read(active/'config/paper_observation_five.local.json')
    if old['universe']!=list(OILS_POOL):raise ValueError('ten-pool migration requires current oils pool')
    download=r8.base.prior.read(inputs/'tq_seed_download.json')
    if download['status']!='COMPLETED' or set(download['products'])!=set(TQ_ADDED):raise ValueError('TQ_DOWNLOAD_INCOMPLETE')
    plan=r8.prepare(client,bundle);stage=bundle/'client';rt=stage/'_strategy_runtime'
    base=Path('data/cache/ten_tq_v1_20261111');(rt/base).mkdir(parents=True)
    manifest=dict(policy=POLICY,source='TQ_DOWNLOADER_UNADJUSTED_M1',historical_known_at='UNKNOWN',
        received_at=download['completed_at'],amount='UNAVAILABLE_NOT_USED_BY_STRATEGY',products={})
    coverage={}
    for cs in TQ_ADDED:
        source=inputs/cs.replace('.','_');row=download['products'][cs];symbol=row['symbol']
        if not row['finished']:raise ValueError('TQ_DOWNLOAD_PARTIAL')
        metadata=r8.base.prior.read(source/'metadata.json')
        if metadata['symbol']!=symbol:raise ValueError('GM_TQ_MAPPING_DISAGREEMENT')
        frame=normalize_seed(source/'tq_seed_original.csv',symbol,download['completed_at'])
        folder=base/cs.replace('.','_');(rt/folder).mkdir()
        frame.to_parquet(rt/folder/'seed.parquet',index=False)
        shutil.copyfile(source/'tq_seed_original.csv',rt/folder/'tq_original.csv')
        atomic(rt/folder/'metadata.json',metadata)
        loader=MarketWorker(dict(evidence_dir=str(rt/folder)),child=True);loader.symbol=symbol
        bars=loader.bars(frame,download['completed_at']);calendar=PaperCalendar(metadata['dates'],metadata['sessions'],cs)
        q=warmup_coverage(bars,calendar,1200);coverage[cs]=q
        manifest['products'][cs]=dict(symbol=symbol,rows=len(frame),seed=str(folder/'seed.parquet'),seed_sha256=sha(rt/folder/'seed.parquet'),
            metadata=str(folder/'metadata.json'),metadata_sha256=sha(rt/folder/'metadata.json'),
            original_csv_sha256=sha(rt/folder/'tq_original.csv'),coverage=q)
    atomic(rt/base/'manifest.json',manifest)
    shutil.copyfile(auth_file,rt/base/'auth.dpapi')
    c=dict(old,universe=list(TEN_POOL),pool_policy_version=TEN_POOL_VERSION,external_market_policy=POLICY,
        tq_feed=dict(python=str(tq_python),auth_file=str(base/'auth.dpapi'),seed_manifest=str(base/'manifest.json'),seed_manifest_sha256=sha(rt/base/'manifest.json')))
    atomic(rt/'config/paper_observation_five.local.json',c)
    load_paper_config(rt/'config/paper_observation_five.local.json',root=rt)
    receipt=build(r8.base.prior.ROOT,stage/'main.py');check=r8.base.self_check(stage)
    (stage/'ops').mkdir(exist_ok=True);shutil.copyfile(r8.base.prior.ROOT/'ops/night_inspect.py',stage/'ops/night_inspect.py')
    plan.update(version=VERSION,new_main_sha256_lf=r8.base.prior.lfsha(stage/'main.py'),new_config_sha256=sha(rt/'config/paper_observation_five.local.json'),
        self_check=check,embedded_files=receipt['embedded_files'],pool_policy_version=TEN_POOL_VERSION,external_market_policy=POLICY,
        coverage=coverage,seeds_qualified=all(x['qualified'] for x in coverage.values()),
        retained=list(OILS_POOL),added=list(TQ_ADDED),seed_manifest_sha256=sha(rt/base/'manifest.json'),
        core_unchanged=old['core_policy_sha256_lf']==c['core_policy_sha256_lf'],
        private_inputs={str(p.relative_to(rt)):sha(p) for p in (rt/base).rglob('*') if p.is_file()},
        old_inspection_sha256=sha(client/'ops/night_inspect.py'),new_inspection_sha256=sha(stage/'ops/night_inspect.py'),
        install_gate='ALL_SEEDS_AND_TESTS_AND_REVIEW_AND_USER_STOP',installed=False,actual_host_loaded=False)
    atomic(bundle/'release_plan.local.json',plan)
    return plan


def install(client,bundle):
    plan=r8.base.prior.read(bundle/'release_plan.local.json');gate=r8.base.prior.read(bundle/'validation_gate.local.json')
    if plan['version']!=VERSION or Path(plan['active_client']).resolve()!=client.resolve():raise ValueError('TQ_RELEASE_IDENTITY')
    if not plan['seeds_qualified'] or not plan.get('prewarm_completed') or not all(gate.get(k) is True for k in ('tests_passed','production_review_passed','native_py310_passed','host_py313_passed','real_feed_consumer_passed')):raise ValueError('TQ_RELEASE_GATES_INCOMPLETE')
    expected_state={Path('state/paper_observation_five/products')/cs.replace('.','_')/name for cs in TQ_ADDED for name in ('core_checkpoint.json','contract_pin.json')}
    expected_meta={Path('evidence/paper_observation_five/products')/cs.replace('.','_')/'market_metadata.local.json' for cs in TQ_ADDED}
    if {Path(p) for p in plan.get('new_state_files',{})}!=expected_state or {Path(p) for p in plan.get('new_metadata_files',{})}!=expected_meta or set(plan.get('prewarm_migrations',{}))!=set(TQ_ADDED):raise ValueError('TQ_PREWARM_COMPONENTS_INCOMPLETE')
    if any(gate.get(k)!=plan[v] for k,v in [('main_sha256_lf','new_main_sha256_lf'),('config_sha256','new_config_sha256'),('seed_manifest_sha256','seed_manifest_sha256')]):raise ValueError('TQ_RELEASE_GATE_FINGERPRINT')
    r8.base.stopped(plan);rt,ev,state,status=r8.base.prior.flat_stopped(client)
    if not state['pause_new']:raise ValueError('INSTALL_REQUIRES_EXISTING_PERSISTENT_PAUSE;DO_NOT_CLEAR_OR_SYNTHESIZE')
    host=r8.base.prior.read(ev/'gm_platform_host.local.json')
    if host['pid']!=plan['host_pid_at_prepare']:raise ValueError('TQ_HOST_CHANGED_REPREPARE_REQUIRED')
    if r8.base.prior.lfsha(client/'main.py')!=plan['old_main_sha256_lf'] or sha(rt/'config/paper_observation_five.local.json')!=plan['old_config_sha256']:raise ValueError('TQ_ACTIVE_BASELINE_CHANGED')
    r8.validate_active_sources(client);stage=bundle/'client';staged=stage/'_strategy_runtime'
    if r8.base.prior.lfsha(stage/'main.py')!=plan['new_main_sha256_lf'] or sha(staged/'config/paper_observation_five.local.json')!=plan['new_config_sha256']:raise ValueError('TQ_STAGED_RELEASE_CHANGED')
    if plan.get('new_inspection_sha256') and (sha(stage/'ops/night_inspect.py')!=plan['new_inspection_sha256'] or sha(client/'ops/night_inspect.py')!=plan['old_inspection_sha256']):raise ValueError('TQ_INSPECTION_SCRIPT_CHANGED')
    for rel,h in plan['private_inputs'].items():
        dest=(rt/rel).resolve()
        if not dest.is_relative_to(rt.resolve()/'data/cache') or sha(staged/rel)!=h or dest.exists():raise ValueError('TQ_PRIVATE_INPUT_CONFLICT')
    protected={str(p.relative_to(rt)):sha(p) for p in (rt/'state/paper_observation_five').rglob('*') if p.is_file() and p.name not in ('execution.json','contract_pin.json','migration.local.json')}
    pins=list((rt/'state/paper_observation_five/products').glob('*/contract_pin.json'))
    if {p.parent.name for p in pins}!={cs.replace('.','_') for cs in OILS_POOL}:raise ValueError('TQ_EXISTING_PRODUCT_STATE_CHANGED')
    for cs in TQ_ADDED:
        if (rt/'state/paper_observation_five/products'/cs.replace('.','_')).exists():raise ValueError('TQ_NEW_STATE_CONFLICT_REQUIRES_REVIEW')
    for rel,h in plan.get('new_state_files',{}).items():
        p=(rt/rel).resolve()
        if not p.is_relative_to(rt.resolve()/'state/paper_observation_five/products') or p.exists() or sha(staged/rel)!=h:raise ValueError('TQ_PREWARM_STATE_CHANGED')
    for rel,h in plan.get('new_metadata_files',{}).items():
        if sha(staged/rel)!=h:raise ValueError('TQ_PREWARM_METADATA_CHANGED')
        dest=rt/rel;old=plan.get('old_metadata_files',{}).get(rel)
        if (dest.exists() and sha(dest)!=old) or (not dest.exists() and old is not None):raise ValueError('TQ_EXISTING_METADATA_CHANGED')
        if (dest.parent/'metadata_before_tq_v1_20261111.local.json').exists():raise ValueError('TQ_METADATA_ARCHIVE_CONFLICT')
    for cs in TQ_ADDED:
        key=cs.replace('.','_');state_path=Path('state/paper_observation_five/products')/key
        cp=r8.base.prior.read(staged/state_path/'core_checkpoint.json');pin=r8.base.prior.read(staged/state_path/'contract_pin.json')
        meta=r8.base.prior.read(staged/'evidence/paper_observation_five/products'/key/'market_metadata.local.json')
        migration=plan['prewarm_migrations'][cs];cache=Path(cp['seed_cache']['path']).resolve()
        if cp['runtime'].get('core_contract')!='CLOSED_FACTS_V2' or pin.get('config_sha256')!=plan['new_config_sha256'] or cp['seed_cache']['symbol']!=pin['symbol'] or meta.get('symbol')!=pin['symbol'] or meta.get('csymbol')!=cs or cp['last_eob']!=migration['last_eob']:
            raise ValueError('TQ_PREWARM_STATE_CONTRACT_CHANGED')
        if not cache.is_relative_to(rt.resolve()/'data/cache/paper_market') or plan['private_inputs'].get(str(cache.relative_to(rt.resolve())))!=cp['seed_cache']['sha256']:
            raise ValueError('TQ_PREWARM_CONSUMED_CACHE_NOT_BOUND')
    r8.base.stopped(plan);r8.base.prior.flat_stopped(client)
    shutil.copytree(rt/'state/paper_observation_five',bundle/'rollback/state_at_install')
    if plan.get('new_inspection_sha256'):shutil.copyfile(client/'ops/night_inspect.py',bundle/'rollback/night_inspect.py')
    for rel in plan['private_inputs']:
        dest=rt/rel;dest.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(staged/rel,dest)
    for rel in plan.get('new_state_files',{}):
        dest=rt/rel;dest.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(staged/rel,dest)
    for rel in plan.get('new_metadata_files',{}):
        dest=rt/rel;dest.parent.mkdir(exist_ok=True,parents=True)
        if dest.exists():shutil.copyfile(dest,dest.parent/'metadata_before_tq_v1_20261111.local.json')
        shutil.copyfile(staged/rel,dest)
    r8.base.replace_main(stage/'main.py',client/'main.py')
    r8.base.replace_main(staged/'config/paper_observation_five.local.json',rt/'config/paper_observation_five.local.json')
    if plan.get('new_inspection_sha256'):r8.base.replace_main(stage/'ops/night_inspect.py',client/'ops/night_inspect.py')
    for p in pins:
        pin=r8.base.prior.read(p);pin['config_sha256']=plan['new_config_sha256'];atomic(p,pin)
    before=json.loads(json.dumps(state));state['config_sha256']=plan['new_config_sha256']
    atomic(rt/'state/paper_observation_five/execution.json',state)
    previous=r8.base.prior.read(rt/'state/paper_observation_five/migration.local.json')
    atomic(rt/'state/paper_observation_five/migration.local.json',dict(kind=TEN_POOL_VERSION,previous=previous,config_sha256=plan['new_config_sha256'],pause_retained=True,
        retained=list(OILS_POOL),added=list(TQ_ADDED),new_state='FRESH_TQ_ONLY_PREWARM;NO_RETIRED_CHECKPOINT_REUSE',prewarm_migrations=plan.get('prewarm_migrations')))
    if any(sha(rt/rel)!=h for rel,h in protected.items()) or {k:v for k,v in before.items() if k!='config_sha256'}!={k:v for k,v in state.items() if k!='config_sha256'}:raise RuntimeError('TQ_PROTECTED_STATE_CHANGED;MANUAL_REVIEW')
    check=r8.base.self_check(client)
    atomic(bundle/'installed.local.json',dict(version=VERSION,installed=True,actual_host_loaded=False,user_start_required=True,
        main_sha256_lf=plan['new_main_sha256_lf'],config_sha256=plan['new_config_sha256'],protected_state_files=len(protected),pause_retained=True,self_check=check,orders_sent=0))


def attach_prewarm(bundle,receipt):
    """Seal consumed TQ inputs without changing runtime state or last_eob."""
    plan=r8.base.prior.read(bundle/'release_plan.local.json');proof=r8.base.prior.read(receipt)
    rt=bundle/'client/_strategy_runtime';active=Path(plan['active_client'])/'_strategy_runtime'
    if not proof.get('passed') or proof.get('production_config_sha256')!=plan['new_config_sha256']:raise ValueError('TQ_PREWARM_NOT_QUALIFIED')
    source=Path(receipt).resolve().parent
    if not source.is_relative_to((rt/'data/cache/tq_preflight').resolve()):raise ValueError('TQ_PREWARM_SOURCE_BOUNDARY')
    state_files={};meta_files={};migrations={}
    c=r8.base.prior.read(rt/'config/paper_observation_five.local.json')
    from futures_trend_quant.live.paper_tq import load_manifest
    manifest=load_manifest(c,rt)
    for cs in TQ_ADDED:
        row=proof['products'][cs];key=cs.replace('.','_')
        if not row['core_ready'] or row['fault'] or not row['coverage']['qualified'] or not row['new_bar_count']:raise ValueError('TQ_PREWARM_CORE_NOT_READY')
        cp_path=source/'state'/key/'core_checkpoint.json';cp=r8.base.prior.read(cp_path)
        if cp['runtime'].get('core_contract')!='CLOSED_FACTS_V2' or pd.Timestamp(cp['last_eob'])<pd.Timestamp(row['core']['last_eob']):raise ValueError('TQ_PREWARM_CHECKPOINT_IDENTITY')
        cache=Path(cp['seed_cache']['path']).resolve()
        if not cache.is_relative_to((rt/'data/cache/paper_market').resolve()) or sha(cache)!=cp['seed_cache']['sha256']:raise ValueError('TQ_PREWARM_CACHE_CHANGED')
        # Seal every consumed minute, not only the original initialization
        # cache: a restart must detect revisions after that initial cutoff too.
        from futures_trend_quant.live.paper_tq_market import tq_rows,merge_same_source
        from futures_trend_quant.live.paper_io import JsonlTail
        from futures_trend_quant.live.paper_data import restore_tq_inputs
        metadata=r8.base.prior.read(rt/manifest['products'][cs]['metadata'])
        calendar=PaperCalendar(metadata['dates'],metadata['sessions'],cs)
        loader=MarketWorker(dict(evidence_dir=str(source/'seal'/key)),child=True);loader.symbol=row['symbol']
        bars=loader.bars(pd.read_parquet(cache),cp['seed_cache']['cutoff'])
        bars=restore_tq_inputs(dict(state_dir=str(source/'state'/key),csymbol=cs),bars,None,cp.get('tq_input_prefix'))
        journal=source/'feed_evidence/tq_feed'/proof['run_id']/(key+'.bars.jsonl')
        for payload in JsonlTail().read(journal):
            if payload.get('symbol')!=row['symbol'] or payload.get('run_id')!=proof['run_id']:raise ValueError('TQ_PREWARM_JOURNAL_IDENTITY')
            bars,_=merge_same_source(bars,tq_rows(payload,row['symbol']),calendar)
        bars=[b for b in bars if pd.Timestamp(b.eob)<=pd.Timestamp(cp['last_eob'])]
        if not bars or pd.Timestamp(bars[-1].eob)!=pd.Timestamp(cp['last_eob']):raise ValueError('TQ_CONSUMED_PREFIX_NOT_SEALED')
        folder=Path('data/cache/paper_market')/('TEN_TQ_'+key+'_'+proof['run_id']+'_FULL_PREFIX')
        (rt/folder).mkdir(exist_ok=False,parents=True)
        frame=pd.DataFrame([dict(symbol=b.symbol,frequency=b.frequency,bob=b.bob,eob=b.eob,open=b.open,high=b.high,low=b.low,close=b.close,volume=b.volume,amount=b.amount,position=b.position) for b in bars])
        frame.to_parquet(rt/folder/'initial_m1.parquet',index=False)
        original_seed_sha=cp['seed_cache']['sha256']
        cp['seed_cache'].update(path=str(active/folder/'initial_m1.parquet'),sha256=sha(rt/folder/'initial_m1.parquet'),cutoff=cp['last_eob'])
        cp.pop('tq_input_prefix',None)  # Committed observed inputs were folded into this exact sealed cache.
        atomic(rt/folder/'input_manifest.json',dict(symbol=row['symbol'],source='TQ_CANONICAL_CONSUMED_PREFIX',rows=len(frame),
            sha256=cp['seed_cache']['sha256'],last_eob=cp['last_eob'],historical_known_at='UNKNOWN',
            original_initial_cache_sha256=original_seed_sha,journal_sha256=sha(journal),runtime_state_changed=False))
        for name in ('initial_m1.parquet','input_manifest.json'):
            plan['private_inputs'][str(folder/name)]=sha(rt/folder/name)
        state=Path('state/paper_observation_five/products')/key
        atomic(rt/state/'core_checkpoint.json',cp)
        atomic(rt/state/'contract_pin.json',dict(symbol=row['symbol'],config_sha256=plan['new_config_sha256']))
        for name in ('core_checkpoint.json','contract_pin.json'):state_files[str(state/name)]=sha(rt/state/name)
        meta=Path('evidence/paper_observation_five/products')/key/'market_metadata.local.json'
        (rt/meta).parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(rt/manifest['products'][cs]['metadata'],rt/meta)
        meta_files[str(meta)]=sha(rt/meta)
        migrations[cs]=dict(source_checkpoint_sha256=sha(cp_path),installed_checkpoint_sha256=sha(rt/state/'core_checkpoint.json'),
            changed_field='seed_cache.path/sha256/cutoff;OBSERVED_INPUTS_SEALED;runtime_AND_last_eob_UNCHANGED',seed_sha256=cp['seed_cache']['sha256'],
            original_initial_cache_sha256=original_seed_sha,last_eob=cp['last_eob'],source='FRESH_TQ_ONLY_PREWARM',historical_known_at='UNKNOWN')
    plan.update(prewarm_completed=True,prewarm_receipt_sha256=sha(receipt),new_state_files=state_files,new_metadata_files=meta_files,prewarm_migrations=migrations)
    plan['old_metadata_files']={rel:sha(active/rel) if (active/rel).exists() else None for rel in meta_files}
    atomic(bundle/'release_plan.local.json',plan)
    return migrations


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','attach-prewarm','install']);p.add_argument('--client',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True);p.add_argument('--receipt',type=Path)
    p.add_argument('--inputs',type=Path);p.add_argument('--auth-file',type=Path);p.add_argument('--tq-python',type=Path);a=p.parse_args()
    if a.mode=='prepare':print(json.dumps(prepare(a.client.resolve(),a.bundle.resolve(),a.inputs.resolve(),a.auth_file.resolve(),a.tq_python.resolve()),ensure_ascii=False,default=str))
    elif a.mode=='attach-prewarm':print(json.dumps(attach_prewarm(a.bundle.resolve(),a.receipt.resolve()),ensure_ascii=False))
    else:install(a.client.resolve(),a.bundle.resolve())
