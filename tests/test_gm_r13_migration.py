from pathlib import Path
from dataclasses import asdict
from copy import deepcopy
from datetime import timedelta
import hashlib,json
import pytest
from test_gm_closed_m1 import GM,bar,POLICY as CLOSED
from futures_trend_quant.live.paper_gm_inputs import POLICY,persist
from futures_trend_quant.live.paper_tq import append
from tools.paper_gm_r13_migration import validate,apply,sha,read
from futures_trend_quant.live.paper_broker import atomic


def fixture(tmp_path):
    client=tmp_path/'client';rt=client/'_strategy_runtime';bundle=tmp_path/'bundle';products={};audit=[]
    for cs,symbol in GM:
        state=rt/'state/paper_observation_five/products'/cs.replace('.','_');c=dict(root=str(rt),state_dir=str(state),csymbol=cs)
        b=bar(symbol);proof=persist(c,[b],{});raw=asdict(b);raw.update(bob=b.bob.isoformat(),eob=b.eob.isoformat())
        journal=state/'gm_core_m1.jsonl';append(journal,dict(policy=CLOSED,csymbol=cs,bar=raw))
        cp=state/'core_checkpoint.json';saved=dict(last_eob=b.eob.isoformat(),runtime=dict(used_evidence_ids=['KEEP'],tombstones=['KEEP_TOMB']),
            seed_cache=proof,gm_input_prefix=dict(path=str(journal),bytes=journal.stat().st_size,sha256=sha(journal)))
        atomic(cp,saved);cache=bundle/'migration'/(cs.replace('.','_')+'.parquet');cache.parent.mkdir(parents=True,exist_ok=True);cache.write_bytes(Path(proof['path']).read_bytes())
        new=dict(proof,path=str(rt/'data/cache/paper_market/r13_migration'/cache.name),parents=dict(seed=proof,initial=proof))
        products[cs]=dict(checkpoint_sha256=sha(cp),cache=cache.relative_to(bundle).as_posix(),gm_input_cache=new)
        audit.append(dict(csymbol=cs,old_second_failed=True,new_second_ready=True,migration_ready=True,runtime_preserved_exact=True,checkpoint_sha256=sha(cp),gm_input_cache=proof))
    sources={}
    for rel in ('futures_trend_quant/live/paper_data.py','futures_trend_quant/live/paper_gm_inputs.py'):
        p=bundle/'client/_strategy_runtime'/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'SOURCE');sources[rel]=sha(p)
    atomic(bundle/'audit.json',dict(products=audit,source_sha256_lf=sources));atomic(bundle/'gm_migration.local.json',dict(audit=str(bundle/'audit.json'),audit_sha256=sha(bundle/'audit.json'),products=products))
    return client,rt,bundle


@pytest.mark.parametrize('tamper',[None,'checkpoint','cache','parent','journal','audit','outside','source'])
def test_bound_migration_changes_only_added_field_or_rejects_before_mutation(tmp_path,tamper):
    client,rt,bundle=fixture(tmp_path);recipe=read(bundle/'gm_migration.local.json');row=recipe['products']['DCE.M']
    cp=rt/'state/paper_observation_five/products/DCE_M/core_checkpoint.json'
    if tamper=='checkpoint':atomic(cp,dict(read(cp),last_eob='CHANGED'))
    if tamper=='cache':(bundle/row['cache']).write_bytes(b'BAD')
    if tamper=='parent':Path(row['gm_input_cache']['parents']['seed']['path']).write_bytes(b'BAD')
    if tamper=='journal':Path(read(cp)['gm_input_prefix']['path']).write_bytes(b'BAD')
    if tamper=='audit':(bundle/'audit.json').write_bytes(b'BAD')
    if tamper=='source':(bundle/'client/_strategy_runtime/futures_trend_quant/live/paper_data.py').write_bytes(b'BAD')
    if tamper=='outside':
        row['gm_input_cache']['path']=str(tmp_path/'outside.parquet');atomic(bundle/'gm_migration.local.json',recipe)
    before={p:p.read_bytes() for p in (rt/'state').rglob('*.json')}
    if tamper:
        with pytest.raises(ValueError):apply(client,bundle)
        assert all(p.read_bytes()==data for p,data in before.items())
    else:
        receipt=apply(client,bundle);assert len(receipt['checkpoints'])==5 and not receipt['pause_or_authorization_changed']
        for p,data in before.items():assert {k:v for k,v in read(p).items() if k!='gm_input_cache'}==json.loads(data)
        with pytest.raises(ValueError,match='CHECKPOINT_CHANGED'):validate(client,bundle)
