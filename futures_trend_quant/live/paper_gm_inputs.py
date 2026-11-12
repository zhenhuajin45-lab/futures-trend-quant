"""Exact consumed GM initialization window; never reconstruct availability clocks."""
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
import hashlib,os,uuid
import pandas as pd
from .paper_broker import atomic

POLICY='GM_CONSUMED_INPUT_CACHE_V1_20261112'


def enabled(c):return c.get('gm_consumed_input_policy')==POLICY


def persist(c,bars,parents,accepted_at=None):
    if not bars:raise ValueError('GM_CONSUMED_CACHE_EMPTY')
    symbol=bars[0].symbol
    if any(b.symbol!=symbol or b.frequency!='60s' for b in bars):raise ValueError('GM_CONSUMED_CACHE_INSTRUMENT')
    base=Path(c['root'])/'data/cache/paper_market'/(c['csymbol'].replace('.','_')+'_consumed')
    base.mkdir(parents=True,exist_ok=True)
    temp=base/(uuid.uuid4().hex+'.tmp.parquet')
    try:
        pd.DataFrame([asdict(b) for b in bars]).to_parquet(temp,index=False)
        digest=hashlib.sha256(temp.read_bytes()).hexdigest();path=base/(digest+'.parquet')
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('GM_CONSUMED_CACHE_COLLISION')
        else:os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)
    proof=dict(policy=POLICY,path=str(path.resolve()),sha256=digest,symbol=symbol,
        csymbol=c['csymbol'],cutoff=bars[-1].eob.isoformat(),rows=len(bars),
        first_eob=bars[0].eob.isoformat(),accepted_at=accepted_at or datetime.now(timezone.utc).isoformat(),
        historical_known_at='UNKNOWN',parents=parents)
    # Unique receipt: a restart never overwrites the original acceptance record.
    atomic(base/(uuid.uuid4().hex+'.manifest.json'),proof)
    return proof


def validate(c,proof,bars):
    from .paper_products import validate_actual
    if proof.get('policy')!=POLICY or proof.get('csymbol')!=c['csymbol']:raise ValueError('GM_CONSUMED_CACHE_IDENTITY')
    validate_actual(c['csymbol'],proof['symbol'])
    if any(b.symbol!=proof['symbol'] for b in bars):raise ValueError('GM_CONSUMED_CACHE_INSTRUMENT')
    if not bars or len(bars)!=proof['rows'] or bars[0].eob.isoformat()!=proof['first_eob'] or bars[-1].eob.isoformat()!=proof['cutoff']:
        raise ValueError('GM_CONSUMED_CACHE_WINDOW')
    return bars
