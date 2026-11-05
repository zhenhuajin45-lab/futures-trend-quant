"""Explicit account-checked handoff. Never modify the original evidence/state."""
from pathlib import Path
import argparse,hashlib,json,shutil,sys,time
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from futures_trend_quant.live.paper_config import load_paper_config
from futures_trend_quant.live.paper_broker import atomic,TERMINAL
from futures_trend_quant.live.paper_transport import SimTransport
from futures_trend_quant.live.paper_products import product_of


def qualified_preflight(new,path,root=ROOT):
    path=Path(path).resolve()
    if not path.is_relative_to(root/'data/cache/five_preflight'):raise ValueError('preflight outside private cache')
    status=json.loads((path/'status.json').read_text(encoding='utf8'))
    manifest=json.loads((path/'run_manifest.json').read_text(encoding='utf8'))
    if status.get('phase')!='READONLY_PREFLIGHT_PASS' or status.get('config_sha256')!=new['config_sha256'] or manifest.get('readonly') is not True or manifest.get('config_sha256')!=new['config_sha256'] or status.get('ready_products')!=5:
        raise ValueError('preflight identity/readiness mismatch')
    inputs={}
    for cs in new['universe']:
        product=status['products'][cs];key=cs.replace('.','_');state=path.parent/'state/products'/key;ev=path/'products'/key
        if not product['ready'] or product['warmup'].get('missing_m1')!=0 or not product['warmup'].get('qualified'):raise ValueError('product preflight not qualified')
        checkpoint=state/'core_checkpoint.json';pin=state/'contract_pin.json';metadata=ev/'market_metadata.local.json'
        cp=json.loads(checkpoint.read_text(encoding='utf8'));p=json.loads(pin.read_text());m=json.loads(metadata.read_text(encoding='utf8'))
        if cp.get('last_eob')!=product['core'].get('last_eob') or p.get('config_sha256')!=new['config_sha256'] or p.get('symbol')!=product['symbol'] or m.get('csymbol')!=cs or m.get('symbol')!=p['symbol']:
            raise ValueError('preflight checkpoint/pin mismatch')
        inputs[cs]=dict(files={name:f.read_bytes() for name,f in [('core_checkpoint.json',checkpoint),('contract_pin.json',pin),('market_metadata.local.json',metadata)]})
    return inputs


def migrate(old,new,snapshot,source_state,target_state,source_evidence,target_evidence,preflight=None):
    if old['account_id']!=new['account_id'] or old['core_policy_sha256_lf']!=new['core_policy_sha256_lf'] or old['base_strategy_sha256_lf']!=new['base_strategy_sha256_lf']:
        raise ValueError('handoff account/core/rules mismatch')
    state=json.loads((source_state/'execution.json').read_text(encoding='utf8'))
    if state['account_id']!=new['account_id'] or state['config_sha256']!=old['config_sha256']:raise ValueError('source execution identity mismatch')
    rows=[snapshot['cash']]+snapshot['positions']+snapshot['orders']+snapshot['active']+snapshot['fills']
    if any(r.get('account_id')!=new['account_id'] for r in rows):raise ValueError('foreign account in handoff')
    if snapshot['active'] or any(i['status'] not in TERMINAL for i in state['intents'].values()):raise ValueError('active or uncertain order blocks handoff')
    known={i.get('order_id') for i in state['intents'].values() if i.get('order_id')}
    if any(o['cl_ord_id'] not in known for o in snapshot['orders']):raise ValueError('unknown broker order')
    expected={}
    for i in state['intents'].values():
        key=i['symbol']+'|'+i['direction'];expected[key]=expected.get(key,0)+(1 if i['effect']==1 else -1)*int(i.get('filled',0))
    actual={p['symbol']+'|'+('bull' if int(p['side'])==1 else 'bear'):int(p['volume']) for p in snapshot['positions'] if int(p['volume'])>0}
    if {k:v for k,v in expected.items() if v}!=actual:raise ValueError('source fills/broker position mismatch')
    if any(product_of(p['symbol']) not in new['universe'] for p in snapshot['positions'] if int(p['volume'])>0):raise ValueError('held product outside new pool')
    if (target_state/'execution.json').exists():raise ValueError('target execution already exists')
    target_state.mkdir(parents=True,exist_ok=True);target_evidence.mkdir(parents=True,exist_ok=True)
    old_hash=hashlib.sha256((source_state/'execution.json').read_bytes()).hexdigest()
    imported=json.loads(json.dumps(state));imported['config_sha256']=new['config_sha256']
    # No risk or operator halt is cleared by scope expansion.
    atomic(target_state/'execution.json',imported)
    child=target_state/'products/DCE_M';child.mkdir(parents=True,exist_ok=True)
    for name in ('core_checkpoint.json',):
        if (source_state/name).exists():shutil.copyfile(source_state/name,child/name)
    if (source_state/'contract_pin.json').exists():
        pin=json.loads((source_state/'contract_pin.json').read_text());pin['config_sha256']=new['config_sha256'];atomic(child/'contract_pin.json',pin)
    dest=target_evidence/'products/DCE_M';dest.mkdir(parents=True,exist_ok=True)
    metadata=json.loads((source_evidence/'market_metadata.local.json').read_text(encoding='utf8'))
    metadata['csymbol']='DCE.M';atomic(dest/'market_metadata.local.json',metadata)
    for name in ('order_events.jsonl','fills.csv','position_recon.jsonl'):
        if (source_evidence/name).exists():shutil.copyfile(source_evidence/name,target_evidence/name)
    imported_warmup={}
    for cs,source in (preflight or {}).items():
        if cs=='DCE.M':continue  # Retain the original running product's exact state.
        key=cs.replace('.','_');st=target_state/'products'/key;ev=target_evidence/'products'/key
        st.mkdir(parents=True,exist_ok=True);ev.mkdir(parents=True,exist_ok=True)
        for name,blob in source['files'].items():
            dest=(ev if name=='market_metadata.local.json' else st)/name;dest.write_bytes(blob)
        imported_warmup[cs]={name:hashlib.sha256(blob).hexdigest() for name,blob in source['files'].items()}
    atomic(target_state/'migration.local.json',dict(source_config_sha256=old['config_sha256'],target_config_sha256=new['config_sha256'],
        source_execution_sha256=old_hash,broker_checked=True,active_orders=0,positive_positions=len(actual),
        existing_trade_ids_and_fee_baseline_preserved=True,original_files_unchanged=True,readonly_warmup_imports=imported_warmup))
    return dict(migrated=True,positive_positions=len(actual),active_orders=0,connectivity_passed=imported['connectivity_passed'],pause_new=imported['pause_new'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--old-config',type=Path,required=True);p.add_argument('--new-config',type=Path,required=True)
    p.add_argument('--preflight-evidence',type=Path)
    a=p.parse_args();old=load_paper_config(a.old_config,root=a.old_config.resolve().parents[1]);new=load_paper_config(a.new_config)
    preflight=qualified_preflight(new,a.preflight_evidence) if a.preflight_evidence else None
    # The operator must have stopped the old account controller after a verified handoff point.
    t=SimTransport(old,a.old_config);t.request('snapshot');deadline=time.monotonic()+15;snapshot=None
    while time.monotonic()<deadline:
        for job,reply in t.drain():
            if reply.get('ok'):snapshot=reply['data']
            else:raise RuntimeError('handoff broker query failed')
        if snapshot is not None:break
        time.sleep(.1)
    if snapshot is None:raise RuntimeError('handoff query timeout')
    result=migrate(old,new,snapshot,Path(old['state_dir']),Path(new['state_dir']),Path(old['evidence_dir']),Path(new['evidence_dir']),preflight)
    print(json.dumps(result))
