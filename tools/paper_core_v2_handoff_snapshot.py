"""One explicit-account SIM read-only snapshot; no order/cancel operations exist here."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,hashlib,json,os,subprocess,uuid
from futures_trend_quant.live.paper_config import load_paper_config
from futures_trend_quant.live.paper_broker import atomic


def snapshot(client):
    root=client/'_strategy_runtime';profile=root/'config/paper_observation_five.local.json'
    c=load_paper_config(profile,root=root);ev=Path(c['evidence_dir'])
    token=json.loads((root/'config/paper_platform_auth.local.json').read_text(encoding='utf-8-sig'))['GM_TOKEN']
    env=dict(os.environ,GM_TOKEN=token)
    request=dict(id=str(uuid.uuid4()),op='snapshot',account_id=c['account_id'],args={})
    with (ev/'readonly_handoff_sdk.local.log').open('a',encoding='utf8') as err:
        process=subprocess.Popen([c['trade_python'],str(root/'futures_trend_quant/live/paper_sim_bridge.py'),str(profile),'READ_ONLY'],
            cwd=root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,encoding='utf8',
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:out,_=process.communicate(json.dumps(request)+'\n',timeout=20)
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=3)
    reply=json.loads(out.strip().splitlines()[-1])
    if not reply.get('ok') or reply.get('id')!=request['id']:raise RuntimeError('read-only SIM handoff query failed')
    atomic(ev/'controlled_handoff_snapshot.local.json',dict(source='GM_NATIVE_SIM_EXPLICIT_ACCOUNT_READ_ONLY',
        received_at=datetime.now(timezone.utc).isoformat(),config_sha256=c['config_sha256'],snapshot=reply['data'],
        reply_sha256=hashlib.sha256(out.encode()).hexdigest(),orders_sent=0))
    print(json.dumps(dict(account_verified=True,positive_positions=sum(int(p['volume'])>0 for p in reply['data']['positions']),
        active_orders=len(reply['data']['active']),orders_sent=0)))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--client',type=Path,required=True);a=p.parse_args();snapshot(a.client.resolve())
