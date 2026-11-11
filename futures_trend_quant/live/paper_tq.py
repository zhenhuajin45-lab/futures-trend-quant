"""Versioned external data contract; stdlib only, including native Python 3.10."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from .paper_io import read_json,shared_open
from .paper_products import TQ_ADDED, TEN_POOL_VERSION, product_of, validate_actual

POLICY='TQ_EXTERNAL_DATA_V1_20261111'
SOURCE='TQ_REALTIME_QUOTE_V1'
TZ=timezone(timedelta(hours=8))


def now_text():return datetime.now(timezone.utc).isoformat()


def clock(value):
    t=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if t.tzinfo is None:raise ValueError('TQ_CLOCK_TIMEZONE_UNKNOWN')
    return t


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(value,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
        # MoveFileExW replacement (os.replace) preserves the destination name
        # for DELETE-sharing readers. Do not use ReplaceFileW's missing-name gap.
        for attempt in range(3):
            try:os.replace(tmp,path);break
            except PermissionError as exc:
                if getattr(exc,'winerror',None) not in (5,32,33) or attempt==2:raise
                time.sleep(.005*(attempt+1))
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def append(path,value):
    """Commit one immutable record. A trailing partial line is never a proof."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a',encoding='utf8',newline='\n') as stream:
        stream.write(json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n');stream.flush();os.fsync(stream.fileno())


def latest_committed(path):
    # Health records are <4KiB. Never substitute a different path or a cached row
    # after a read failure; the newest newline-committed record defines progress.
    with shared_open(path) as stream:
        stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-65536));raw=stream.read()
    complete=raw.split(b'\n')[:-1]
    if not complete:raise ValueError('TQ_PROGRESS_NOT_COMMITTED')
    return json.loads(complete[-1])


def enabled(c):return c.get('external_market_policy')==POLICY


def external_product(c,cs):return enabled(c) and cs in TQ_ADDED


def validate_native_policy(c):
    from .paper_symbol_health import POLICY as SYMBOL_POLICY
    if c.get('symbol_market_policy') is not None:
        if c['symbol_market_policy']!=SYMBOL_POLICY or c.get('clock_policy_version')!='PAPER_CLOCK_HEALTH_V1_20261109' or c.get('execution_policy_version')!='PAPER_EXEC_V5':raise ValueError('SYMBOL_ISOLATION_ACCOUNT_GUARDS_REQUIRED')
    if c.get('pool_policy_version')==TEN_POOL_VERSION:
        if not enabled(c) or c.get('quote_policy_version')!='GM_HOST_TICK_PRIMARY_V1_20261109':raise ValueError('TQ_NATIVE_SOURCE_POLICY_REQUIRED')
    elif c.get('external_market_policy') is not None or c.get('tq_feed'):
        raise ValueError('TQ_NATIVE_POOL_POLICY_MISMATCH')


def validate_config(c,root):
    validate_native_policy(c)
    external=c.get('external_market_policy')
    if external is None:
        if c.get('tq_feed') or c.get('pool_policy_version')==TEN_POOL_VERSION:raise ValueError('TQ_PROFILE_MISSING')
        return
    if external!=POLICY or c.get('pool_policy_version')!=TEN_POOL_VERSION:raise ValueError('TQ_POLICY_POOL_MISMATCH')
    if c.get('quote_policy_version')!='GM_HOST_TICK_PRIMARY_V1_20261109':raise ValueError('TQ_REQUIRES_FINAL_QUOTE_POLICY')
    feed=c['tq_feed'];root=Path(root).resolve()
    for name in ('seed_manifest','auth_file'):
        p=(root/feed[name]).resolve()
        if not p.is_relative_to(root/'data/cache'):raise ValueError('TQ_PRIVATE_PATH_BOUNDARY')
        if not p.is_file():raise ValueError('TQ_PRIVATE_INPUT_MISSING')
    manifest=load_manifest(c,root)
    if set(manifest['products'])!=set(TQ_ADDED):raise ValueError('TQ_MANIFEST_POOL_MISMATCH')
    if not Path(feed['python']).is_absolute() or not Path(feed['python']).is_file():raise ValueError('TQ_INTERPRETER_MISSING')


def load_manifest(c,root):
    feed=c['tq_feed'];p=(Path(root)/feed['seed_manifest']).resolve()
    if sha(p)!=feed['seed_manifest_sha256']:raise ValueError('TQ_SEED_MANIFEST_CHANGED')
    m=read_json(p)
    if m.get('policy')!=POLICY or m.get('source')!='TQ_DOWNLOADER_UNADJUSTED_M1':raise ValueError('TQ_SEED_SOURCE_IDENTITY')
    for cs,row in m['products'].items():
        validate_actual(cs,row['symbol'])
        if cs not in TQ_ADDED:raise ValueError('TQ_UNAUTHORIZED_PRODUCT')
        for key in ('seed','metadata'):
            p=(Path(root)/row[key]).resolve()
            if not p.is_relative_to(Path(root).resolve()/'data/cache') or sha(p)!=row[key+'_sha256']:raise ValueError('TQ_SEED_FILE_CHANGED')
        metadata=read_json(Path(root)/row['metadata'])
        if metadata.get('csymbol')!=cs or metadata.get('symbol')!=row['symbol']:raise ValueError('TQ_METADATA_PIN_MISMATCH')
        if row.get('rows',0)<22000:raise ValueError('TQ_SEED_TOO_SHORT')
    return m


def validate_envelope(row,c,symbol,now,max_age=3):
    if row.get('policy')!=POLICY or row.get('config_sha256')!=c['config_sha256'] or row.get('run_id')!=c['tq_run_id'] or row.get('symbol')!=symbol:
        raise ValueError('TQ_FEED_IDENTITY_MISMATCH')
    if not 0<=(clock(now)-clock(row['updated_at'])).total_seconds()<=max_age:raise ValueError('TQ_FEED_PROGRESS_STALE_OR_FUTURE')


def current_feed_proof(root,c,digest,now):
    started=time.monotonic()
    manifest=read_json(Path(root)/c['evidence_dir']/'run_manifest.json')
    run_id=manifest.get('run_id','')
    if not run_id or any(x not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for x in run_id):raise ValueError('TQ_HOST_GENERATION_INVALID')
    proof=latest_committed(Path(root)/c['evidence_dir']/'tq_feed'/run_id/'health.jsonl')
    checked=clock(now)+timedelta(seconds=time.monotonic()-started)
    if proof.get('config_sha256')!=digest or proof.get('policy')!=POLICY or proof.get('status')!='RUNNING':
        raise ValueError('TQ_FINAL_GENERATION_MISMATCH')
    if manifest.get('run_id')!=proof.get('run_id') or manifest.get('config_sha256')!=digest:raise ValueError('TQ_FINAL_HOST_GENERATION_MISMATCH')
    if not 0<=(checked-clock(proof['updated_at'])).total_seconds()<=3:raise ValueError('TQ_FINAL_PROGRESS_STALE')
    return proof


def final_source(root,c,digest,symbol,q,now,opening=True):
    cs=product_of(symbol)
    if not external_product(c,cs):
        if q.get('source')==SOURCE:raise ValueError('TQ_SOURCE_NOT_AUTHORIZED_FOR_PRODUCT')
        return
    if q.get('source')!=SOURCE:raise ValueError('TQ_REQUIRED_SOURCE_MISMATCH')
    proof=current_feed_proof(root,c,digest,now)
    if q.get('source_run_id')!=proof.get('run_id'):raise ValueError('TQ_FINAL_GENERATION_MISMATCH')
    from .paper_symbol_health import enabled as isolated
    if opening and isolated(c) and proof.get('product_status',{}).get(cs,{}).get('ready') is not True:raise ValueError('TQ_SYMBOL_SOURCE_NOT_READY')
    if proof.get('contracts',{}).get(cs)!=symbol:raise ValueError('TQ_FINAL_PIN_MISMATCH')
    if opening and proof.get('main_mapping',{}).get(cs)!=symbol:raise ValueError('TQ_MAIN_MAPPING_CHANGED_OR_UNKNOWN')


def quote_values(raw,spec,run_id):
    event=datetime.fromisoformat(raw['datetime']).replace(tzinfo=TZ)
    values={k:float(raw[k]) for k in ('last_price','bid_price1','ask_price1','bid_volume1','ask_volume1')}
    if not all(math.isfinite(v) for v in values.values()):raise ValueError('TQ_NONFINITE_QUOTE')
    return dict(created_at=event.isoformat(),last=values['last_price'],tick=float(spec['price_tick']),
        bid=values['bid_price1'],ask=values['ask_price1'],bid_size=values['bid_volume1'],ask_size=values['ask_volume1'],
        source=SOURCE,source_run_id=run_id)
