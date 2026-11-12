"""Explicit stopped-host migration, bound to a frozen full-process replay receipt."""
from pathlib import Path
import hashlib,json,shutil
from datetime import datetime,timezone
from futures_trend_quant.live.paper_broker import atomic
from futures_trend_quant.live.paper_gm_inputs import POLICY

GM=('DCE.M','SHFE.AU','CZCE.OI','DCE.Y','DCE.P')


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))


def validate(client,bundle):
    recipe=read(bundle/'gm_migration.local.json');rt=client/'_strategy_runtime'
    audit=Path(recipe['audit']);expected=recipe['audit_sha256']
    if sha(audit)!=expected:raise ValueError('R13_MIGRATION_AUDIT_CHANGED')
    evidence=read(audit)
    source_names={'futures_trend_quant/live/paper_data.py','futures_trend_quant/live/paper_gm_inputs.py'}
    if set(evidence.get('source_sha256_lf',{}))!=source_names:raise ValueError('R13_MIGRATION_SOURCE_AUDIT_REQUIRED')
    for rel,digest in evidence['source_sha256_lf'].items():
        source=bundle/'client/_strategy_runtime'/rel
        if hashlib.sha256(source.read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=digest:raise ValueError('R13_MIGRATION_SOURCE_CHANGED')
    if {r['csymbol'] for r in evidence['products']}!=set(GM) or not all(
        r.get('old_second_failed') and r.get('new_second_ready') and r.get('migration_ready') and r.get('runtime_preserved_exact') for r in evidence['products']):
        raise ValueError('R13_MIGRATION_RESTART_NOT_PROVEN')
    if set(recipe['products'])!=set(GM):raise ValueError('R13_MIGRATION_FIVE_REQUIRED')
    for cs,row in recipe['products'].items():
        cp=rt/'state/paper_observation_five/products'/cs.replace('.','_')/'core_checkpoint.json'
        audited=next(r for r in evidence['products'] if r['csymbol']==cs)
        if sha(cp)!=row['checkpoint_sha256'] or row['checkpoint_sha256']!=audited['checkpoint_sha256']:raise ValueError('R13_MIGRATION_CHECKPOINT_CHANGED')
        proof=row['gm_input_cache'];cache=(bundle/row['cache']).resolve();target=Path(proof['path']).resolve()
        if any(proof.get(k)!=audited['gm_input_cache'].get(k) for k in ('policy','csymbol','symbol','sha256','rows','first_eob','cutoff','historical_known_at')):
            raise ValueError('R13_MIGRATION_WINDOW_CHANGED')
        if not cache.is_relative_to(bundle.resolve()/'migration') or sha(cache)!=proof['sha256'] or proof['sha256']!=audited['gm_input_cache']['sha256']:
            raise ValueError('R13_MIGRATION_CACHE_CHANGED')
        if not target.is_relative_to(rt.resolve()/'data/cache/paper_market/r13_migration') or proof.get('policy')!=POLICY or proof.get('csymbol')!=cs:
            raise ValueError('R13_MIGRATION_SCOPE')
        saved=read(cp)
        if saved.get('gm_input_cache') is not None:raise ValueError('R13_MIGRATION_ALREADY_PRESENT')
        for parent in (proof['parents']['seed'],proof['parents']['initial']):
            source=Path(parent['path']).resolve()
            if not source.is_relative_to(rt.resolve()/'data/cache/paper_market') or sha(source)!=parent['sha256']:raise ValueError('R13_MIGRATION_PARENT_CHANGED')
        journal=saved['gm_input_prefix'];raw=Path(journal['path']).read_bytes()
        if hashlib.sha256(raw[:journal['bytes']]).hexdigest()!=journal['sha256']:raise ValueError('R13_MIGRATION_JOURNAL_CHANGED')
    return recipe


def apply(client,bundle):
    recipe=validate(client,bundle);rt=client/'_strategy_runtime';changed={}
    # The caller has just repeated stopped/account gates. No authorization or
    # execution state is touched here. Every old checkpoint is already archived.
    for cs,row in recipe['products'].items():
        proof=row['gm_input_cache'];target=Path(proof['path']);target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and sha(target)!=proof['sha256']:raise ValueError('R13_MIGRATION_TARGET_CHANGED')
        if not target.exists():
            temporary=target.with_suffix('.staged');shutil.copyfile(bundle/row['cache'],temporary)
            if sha(temporary)!=proof['sha256']:raise ValueError('R13_MIGRATION_COPY_CHANGED')
            temporary.replace(target)
    for cs,row in recipe['products'].items():
        cp=rt/'state/paper_observation_five/products'/cs.replace('.','_')/'core_checkpoint.json';before=read(cp)
        if sha(cp)!=row['checkpoint_sha256']:raise ValueError('R13_MIGRATION_CHECKPOINT_RACED')
        atomic(cp,dict(before,gm_input_cache=row['gm_input_cache']))
        after=read(cp)
        if {k:v for k,v in after.items() if k!='gm_input_cache'}!=before:raise RuntimeError('R13_MIGRATION_STATE_CHANGED')
        changed[cp.relative_to(rt).as_posix()]=dict(before_sha256=row['checkpoint_sha256'],after_sha256=sha(cp))
    receipt=dict(at=datetime.now(timezone.utc).isoformat(),checkpoints=changed,pause_or_authorization_changed=False,
        runtime_last_eob_and_consumed_ids_exact=True,original_inputs_retained=True,audit_sha256=recipe['audit_sha256'])
    atomic(bundle/'gm_migrated.local.json',receipt);return receipt
