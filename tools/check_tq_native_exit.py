"""Run the actual native bridge under Python 3.10 with a synthetic SDK only.

No SDK login, real account, order or network operation is possible in this check.
The helper reuses the existing stdin bridge fixture without importing pandas/pytest.
"""
from pathlib import Path
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace,ModuleType
import ast
import hashlib
import io
import json
import os
import sys
import tempfile
from futures_trend_quant.live import paper_sim_bridge as bridge
from futures_trend_quant.live.paper_tq import POLICY,SOURCE,atomic,append
from futures_trend_quant.live.paper_products import TEN_POOL,TEN_POOL_VERSION,product_of

ACCOUNT='00000000-0000-0000-0000-000000000001'


class Patch:
    def __init__(self):self.undo=[]
    def setitem(self,obj,key,value):
        had=key in obj;old=obj.get(key);self.undo.append(lambda:obj.__setitem__(key,old) if had else obj.pop(key,None));obj[key]=value
    def setattr(self,obj,key,value):
        old=getattr(obj,key);self.undo.append(lambda:setattr(obj,key,old));setattr(obj,key,value)
    def setenv(self,key,value):self.setitem(os.environ,key,value)
    def close(self):
        for undo in reversed(self.undo):undo()


def main(output,all_products=False,isolation=False,startup=False):
    root=Path(__file__).resolve().parents[1]
    tree=ast.parse((root/'tests/test_paper_bridge.py').read_text(encoding='utf8'))
    functions=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('invoke','args','req')],type_ignores=[])
    ns=dict(globals(),digest=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest())
    exec(compile(functions,'EXISTING_SYNTHETIC_BRIDGE_FIXTURE','exec'),ns)
    work=Path(tempfile.mkdtemp(prefix='ftq_tq_native_'));results=[]
    symbols=('CZCE.MA701','DCE.eg2701','DCE.jd2612','CZCE.CJ701','SHFE.ag2702')
    if all_products:symbols+=('DCE.m2701','SHFE.au2612','CZCE.OI701','DCE.y2701','DCE.p2701')
    for symbol in symbols:
        external=product_of(symbol) in ('CZCE.MA','DCE.EG','DCE.JD','CZCE.CJ','SHFE.AG')
        for side in (1,2):
            cases=('valid','old_generation','stale_quote','bad_size','pause','expired_signal','wrong_account')
            if isolation:cases+=('unrelated_fault','symbol_not_ready','wrong_symbol_proof','clock_untrusted','clock_stale','clock_sampler_wrong','clock_rtt','service_stale','other_active_intent')
            for case in cases:
                if case=='old_generation' and not external:continue
                now=datetime.now(timezone.utc);direction='bull' if side==1 else 'bear'
                a=ns['args'](now);a.update(symbol=symbol,side=side,anchor=99 if side==1 else 103,price=101 if side==1 else 100,
                    stop=97 if side==1 else 104,anchor_bar_id=symbol+'|900s|'+(now-timedelta(minutes=30)).isoformat(),
                    permission_policy='PAPER_ENTRY_V5',path='D',core_evidence_id='SYNTHETIC',entry_permission=True,
                    permission_direction=direction,permission_eob=now.isoformat(),evidence_created_at=now.isoformat(),
                    first_eligible_at=now.isoformat(),decision_at=now.isoformat(),expires_at=(now+timedelta(seconds=30)).isoformat(),
                    reference_opponent_price=101 if side==1 else 100)
                a['entry_policy_proof']=dict(policy='PAPER_ENTRY_V5',path='D',evidence_id='SYNTHETIC',eob=now.isoformat(),allowed=True,reasons=[],hist=1 if side==1 else -1,
                    close_side='above' if side==1 else 'below',slope='up' if side==1 else 'down',opposite_divergence=False,
                    closed_h1_known=True,calendar_qualified=True,h1_absolute='bull_strong' if side==1 else 'bear_strong',
                    h1_source=dict(closed=True,calendar_qualified=True,entry_eob=(now-timedelta(hours=1)).isoformat()))
                if case=='expired_signal':a['expires_at']=(now-timedelta(seconds=1)).isoformat()
                path=work/(symbol.replace('.','_')+'_'+str(side)+'_'+case);path.mkdir()
                def before(folder,profile):
                    digest=ns['digest'](profile)
                    state=dict(config_sha256=digest,account_id=ACCOUNT,pause_new=case=='pause',halt_reason='',loss_cooldown={})
                    if isolation:
                        intent=dict(id='R1',effect=1,status=10,order_id='',filled=0,native_submit_invoked=False,evidence_id='BOUND',**{k:a.get(k) for k in ('symbol','side','price','stop','path','core_evidence_id')})
                        state['intents']={'R1':intent}
                        if case=='other_active_intent':state['intents']['OTHER']=dict(status=10,cancel_outcome_unknown=True)
                    atomic(folder/'state/execution.json',state)
                    q=dict(created_at=(now-timedelta(seconds=4) if case=='stale_quote' else now).isoformat(),last=101 if side==1 else 100,
                        bid=100,ask=101,bid_size=5,ask_size=0 if case=='bad_size' else 5,tick=1,
                        source=SOURCE if external else 'GM_HOST_TICK_CALLBACK',source_run_id='OLD' if case=='old_generation' else 'GEN')
                    atomic(folder/'ev/latest_quotes'/(symbol+'.json'),dict(config_sha256=digest,symbol=symbol,host_run_id='GEN',received_at=now.isoformat(),quote=q))
                    atomic(folder/'ev/tq_feed/current_run.json',dict(policy=POLICY,config_sha256=digest,run_id='GEN',status='RUNNING',updated_at=now.isoformat(),contracts={product_of(symbol):symbol}))
                    manifest=dict(run_id='GEN',config_sha256=digest)
                    if isolation:
                        from futures_trend_quant.live.paper_symbol_health import SERVICE_POLICY
                        from futures_trend_quant.live.paper_clock_health import collect,SAMPLER_POLICY
                        manifest['service_policy_version']=SERVICE_POLICY
                        clock=collect(digest,'GEN',lambda s:dict(source=s,offset_seconds=.8 if case=='clock_untrusted' else .01,rtt_seconds=.01,stratum=2))
                        clock.update(sampler_policy=SAMPLER_POLICY,sampler_sequence=1)
                        if case=='clock_stale':clock['sample_monotonic']-=91
                        elif case=='clock_sampler_wrong':clock.pop('sampler_policy')
                        elif case=='clock_rtt':clock['samples'][0]['rtt_seconds']=1.001
                        atomic(folder/'ev/clock_health.local.json',clock)
                        own=dict(symbol=symbol,ready=case not in ('symbol_not_ready','wrong_symbol_proof'))
                        if case=='wrong_symbol_proof':own.update(symbol='OTHER',ready=True)
                        proof=dict(policy=SERVICE_POLICY,run_id='GEN',config_sha256=digest,at=(now-timedelta(seconds=4) if case=='service_stale' else now).isoformat(),account_verified_at=now.isoformat(),entry_gate_ready=True,service_ready=True,active=None,
                            symbol_readiness={symbol:own,'UNRELATED':dict(symbol='UNRELATED',ready=case!='unrelated_fault')})
                        atomic(folder/'ev/service_health.local.json',proof)
                    atomic(folder/'ev/run_manifest.json',manifest)
                    append(folder/'ev/tq_feed/GEN/health.jsonl',dict(policy=POLICY,config_sha256=digest,run_id='GEN',status='RUNNING',updated_at=now.isoformat(),contracts={product_of(symbol):symbol},main_mapping={product_of(symbol):symbol},product_status={product_of(symbol):dict(ready=True)}))
                record=dict(csymbol=product_of(symbol),multiplier=10,margin_ratio=.07,tick=1,entry_windows=[(now.timestamp()-60,now.timestamp()+60)],trade_windows=[(now.timestamp()-60,now.timestamp()+60)])
                profile=dict(schema_version=2,strategy_name='S0_SHORT_PAPER_CORE_V2',entry_policy_version='PAPER_ENTRY_V5',execution_policy_version='PAPER_EXEC_V5',
                    state_dir='state',evidence_dir='ev',market_registry='market.json',universe=list(TEN_POOL),pool_policy_version=TEN_POOL_VERSION,
                    quote_policy_version='GM_HOST_TICK_PRIMARY_V1_20261109',external_market_policy=POLICY,max_positions=2)
                if isolation:
                    from futures_trend_quant.live.paper_symbol_health import POLICY as SYMBOL_POLICY
                    from futures_trend_quant.live.paper_clock_health import SAMPLER_POLICY
                    profile.update(symbol_market_policy=SYMBOL_POLICY,clock_policy_version='PAPER_CLOCK_HEALTH_V1_20261109',clock_sampler_policy=SAMPLER_POLICY)
                    if startup:
                        from futures_trend_quant.live.paper_clock_health import STARTUP_POLICY
                        profile.update(clock_startup_policy=STARTUP_POLICY,tq_input_wait_policy='TQ_INPUT_WAIT_V2_20261112',notice_evaluation_policy='NOTICE_CAUSAL_PREFILTER_V1_20261112')
                patch=Patch()
                try:
                    replies,calls=ns['invoke'](path,patch,[ns['req']('submit',account='OTHER' if case=='wrong_account' else ACCOUNT,arguments=a)],profile_extra=profile,
                        contracts={symbol:record},before_serve=before)
                finally:patch.close()
                expected=case in ('valid','unrelated_fault');reply=replies[0]
                ok=bool(reply['ok'])==expected and bool(reply['native_submit_invoked'])==expected and sum(n=='submit' for n,v in calls)==int(expected)
                if expected:ok=ok and reply['final_execution_quote']['source']==(SOURCE if external else 'GM_HOST_TICK_CALLBACK')
                results.append(dict(symbol=symbol,side=side,case=case,passed=ok,native_stub_invoked=reply['native_submit_invoked'],reason=reply.get('reason')))
    receipt=dict(python=sys.version,scenarios=len(results),passed=all(r['passed'] for r in results),network_or_real_orders=False,startup_policies=startup,results=results)
    atomic(output,receipt);print(json.dumps(dict(passed=receipt['passed'],scenarios=len(results),output=str(output))))
    return receipt['passed']


if __name__=='__main__':raise SystemExit(0 if main(Path(sys.argv[1]),'--all-products' in sys.argv,'--isolation' in sys.argv,'--startup' in sys.argv) else 1)
