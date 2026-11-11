"""Build a readable, source-complete GM main.py. Never package local data or secrets."""
from pathlib import Path
import hashlib,json,argparse
ROOT=Path(__file__).resolve().parents[1]


BOOT=r'''
import hashlib,json,os,sys,tempfile
from pathlib import Path
os.environ['PYTHONUTF8']='1'  # Native SIM child JSON uses the same UTF-8 protocol from client launches.
CLIENT_DIR=Path(__file__).resolve().parent
RUNTIME_ROOT=CLIENT_DIR/'_strategy_runtime'
for relative,source in EMBEDDED_SOURCES.items():
    target=RUNTIME_ROOT/relative
    if not target.resolve().is_relative_to(RUNTIME_ROOT.resolve()):raise ValueError('invalid embedded path')
    blob=source.encode('utf8')
    if hashlib.sha256(blob).hexdigest()!=SOURCE_SHA256[relative]:raise ValueError('embedded source hash changed: '+relative)
    if not target.exists() or target.read_bytes()!=blob:
        target.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.source_')
        with os.fdopen(fd,'wb') as f:f.write(blob);f.flush();os.fsync(f.fileno())
        os.replace(tmp,target)
sys.path.insert(0,str(RUNTIME_ROOT))
# The client strategy owns all project code and private state. External project overrides are ignored.
os.environ['GM_PAPER_CONFIG']=str(RUNTIME_ROOT/'config/paper_observation_five.local.json')
os.environ.pop('FUTURES_TREND_QUANT_ROOT',None)
from futures_trend_quant.live.paper_config import frozen_core_digest
assert frozen_core_digest(RUNTIME_ROOT)==EXPECTED_CORE_SHA256,'core/holding policy fingerprint mismatch'
if '--self-check' in sys.argv:
    import ast
    for name,source in EMBEDDED_SOURCES.items():
        if name.endswith('.py'):ast.parse(source,filename=name)
    from futures_trend_quant.core.pipeline import run_core_step
    from futures_trend_quant.live.paper_five import FiveRunner
    from futures_trend_quant.live.gm_paper_host import init,on_timer,on_tick,on_bar,on_error,on_shutdown,on_market_data_connected,on_trade_data_connected,on_market_data_disconnected,on_trade_data_disconnected
    from futures_trend_quant.live.paper_notify import message
    if '--check-config' in sys.argv:
        from futures_trend_quant.live.paper_config import load_paper_config
        c=load_paper_config(os.environ['GM_PAPER_CONFIG']);assert c['root']==str(RUNTIME_ROOT)
    print(json.dumps(dict(self_contained_check='PASS',embedded_files=len(EMBEDDED_SOURCES),runtime_root=str(RUNTIME_ROOT),
        core_fingerprint_verified=True,external_project_required=False,private_config_checked='--check-config' in sys.argv)))
    raise SystemExit(0)
# multiprocessing spawn replays this module as __mp_main__. Its data/core/PA/
# clock targets import only their own dependencies; they are not another host.
if __name__!='__mp_main__':
    from gm.api import MODE_LIVE,run
    from futures_trend_quant.live.gm_paper_host import init,on_timer,on_tick,on_bar,on_error,on_shutdown,launch_parameters,on_market_data_connected,on_trade_data_connected,on_market_data_disconnected,on_trade_data_disconnected
if __name__=='__main__':
    strategy_id,token=launch_parameters(sys.argv[1:],RUNTIME_ROOT,CLIENT_DIR/'paper_platform.local.json')
    os.environ['GM_TOKEN']=token
    run(strategy_id=strategy_id,filename=Path(__file__).as_posix(),mode=MODE_LIVE,token=token)
'''


def build(root,output):
    paths=[]
    for folder in ('futures_trend_quant','backtest','tools'):
        paths.extend(p for p in (root/folder).rglob('*.py') if '__pycache__' not in p.parts and 'supplemental_tq' not in p.parts
                     and p.name not in ('futures_trend_review.py','futures_trend_publish.py'))  # offline-only research, never a client dependency
    paths.extend(root/'config'/name for name in ('strategy.json','paper_observation.example.json','paper_observation_five.example.json','paper_feishu.example.json','paper_observation_usage.json','price_action_shadow.example.json'))
    paths.extend(root/'deploy'/name for name in ('gm_paper_main.py','gm_paper_five_main.py'))
    # Only the DPAPI reader is a runtime dependency; offline collectors stay outside the package.
    paths.append(root/'tools/supplemental_tq/credentials.py')
    sources={p.relative_to(root).as_posix():p.read_text(encoding='utf-8-sig').replace('\r\n','\n') for p in sorted(paths)}
    # Namespace roots are explicit so imports can never fall through to an external checkout.
    for folder in ('futures_trend_quant','backtest','tools'):sources.setdefault(folder+'/__init__.py','')
    import sys
    sys.path.insert(0,str(root))
    from futures_trend_quant.live.paper_config import frozen_core_digest
    parts=['# coding=utf-8\n# Generated complete GM S0 strategy. All first-party Python sources are embedded below.\n# Private credentials, market data and account state are deliberately NOT embedded.\nEMBEDDED_SOURCES={}\n']
    for name,source in sorted(sources.items()):
        delimiter=next((d for d in ("'''",'"""') if d not in source),None)
        literal=('r'+delimiter+source+delimiter) if delimiter and not source.endswith('\\') else repr(source)
        parts.append('\n# ===== '+name+' =====\nEMBEDDED_SOURCES['+repr(name)+']='+literal+'\n')
    hashes={n:hashlib.sha256(s.encode()).hexdigest() for n,s in sources.items()}
    parts.append('\nSOURCE_SHA256='+repr(hashes)+'\nEXPECTED_CORE_SHA256='+repr(frozen_core_digest(root))+'\n')
    parts.append(BOOT)
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(''.join(parts),encoding='utf8')
    return dict(embedded_files=len(sources),bytes=output.stat().st_size,sha256=hashlib.sha256(output.read_bytes()).hexdigest())


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=ROOT/'deploy/gm_paper_standalone_main.py');a=p.parse_args()
    print(json.dumps(build(ROOT,a.output)))
