"""Run the real SDK-free native final exit with synthetic vendor functions.

Compatible with the actual production Python 3.10. Never imports real gmtrade,
logs into an account, creates a controller, or sends a broker request.
"""
from pathlib import Path
from types import ModuleType, SimpleNamespace
from datetime import datetime, timedelta, timezone
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile


def exercise(problem=None, direction='bull', policy='PAPER_ENTRY_V5'):
    account='00000000-0000-4000-8000-000000000008';calls=[]
    api=ModuleType('gmtrade.api');trade=ModuleType('gmtrade.api.trade');storage=ModuleType('gmtrade.api.storage')
    storage.ctx=SimpleNamespace(logined_accounts={});trade.c_status_fail=lambda status,name:status!=0
    api.trade=trade;api.set_token=lambda _:None;api.set_endpoint=lambda _:None
    api.account=lambda account_id:SimpleNamespace(account_id=account_id)
    api.login=lambda a:storage.ctx.logined_accounts.update({a.account_id:a})
    api.get_cash=lambda account:dict(account_id=account,nav=1000000.,available=1000000.)
    api.get_positions=lambda account:[];api.get_orders=lambda account:[];api.get_execution_reports=lambda account:[]
    api.get_unfinished_orders=lambda account:[dict(account_id=account,symbol='DCE.m2701',volume=1)] if problem=='active' else []
    def submit(**kw):
        calls.append(kw);return [dict(account_id=kw['account'],cl_ord_id='SYNTHETIC',symbol=kw['symbol'],volume=kw['volume'])]
    api.order_volume=submit
    parent=ModuleType('gmtrade');parent.api=api
    for name,value in [('gmtrade',parent),('gmtrade.api',api),('gmtrade.api.trade',trade),('gmtrade.api.storage',storage)]:sys.modules[name]=value
    import google.protobuf.json_format as fmt
    fmt.MessageToDict=lambda x,**kw:x if isinstance(x,dict) else vars(x)
    os.environ['GM_TOKEN']='SYNTHETIC_ONLY'
    from futures_trend_quant.live import paper_sim_bridge as bridge
    with tempfile.TemporaryDirectory(prefix='r8_native_') as temp:
        root=Path(temp);now=datetime.now(timezone.utc);long=direction=='bull'
        def put(p,d):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(d),encoding='utf8')
        proof=root/'account.json';put(proof,dict(account_id=account,endpoint='api.myquant.cn:9000',source='GM_OFFICIAL_SIM_ACCOUNT_CASH_ID_MATCH',user_designated_futures_sim=True))
        c=dict(endpoint='api.myquant.cn:9000',live_orders_enabled=False,paper_orders_enabled=True,transport='OFFICIAL_SIM_GMTRADE',account_binding='account.json',account_binding_sha256=hashlib.sha256(proof.read_bytes()).hexdigest(),strategy_name='S0_SHORT_PAPER_CORE_V2',execution_policy_version='PAPER_EXEC_V5',entry_policy_version='PAPER_ENTRY_V5',state_dir='state',evidence_dir='ev')
        if policy!='PAPER_ENTRY_V5':c.pop('entry_policy_version')
        config=root/'config.json';put(config,c);digest=hashlib.sha256(config.read_bytes()).hexdigest()
        put(root/'state/execution.json',dict(config_sha256=digest,account_id=account,pause_new=problem=='pause',halt_reason='',loss_cooldown={}))
        t=now-timedelta(seconds=4) if problem=='stale_quote' else now
        quote=dict(created_at=t.isoformat(),bid=100.,ask=101.,last=100.5,bid_size=2,ask_size=2,tick=1.,source='GM_BULK_CURRENT_POLL')
        put(root/'ev/latest_quotes/DCE.m2701.json',dict(config_sha256=digest,symbol='DCE.m2701',received_at=t.isoformat(),quote=quote))
        first=now-timedelta(seconds=31) if problem=='expiry' else now
        start='DCE.m2701|300s|'+(first-timedelta(minutes=15)).isoformat();confirm='DCE.m2701|300s|'+(first-timedelta(minutes=5)).isoformat()
        args=dict(symbol='DCE.m2701',volume=1,side=1 if long else 2,position_effect=1,price=101. if long else 100.,stop=97. if long else 104.,multiplier=10.,margin_ratio=.07,anchor=99. if long else 102.,anchor_bar_id='DCE.m2701|900s|'+(first-timedelta(minutes=30)).isoformat(),anchor_confirmed_at=(first-timedelta(minutes=15)).isoformat(),anchor_episode_id='SYNTHETIC',trend_episode_id='SYNTHETIC',anchor_validated=True,signal_at=first.isoformat(),quote_event_at=now.isoformat(),quote_received_at=now.isoformat(),quote_bid=100.,quote_ask=101.,tick=1.,day_nav=1000000.,peak_nav=1000000.,path='B',core_evidence_id='E',evidence_created_at=(first-timedelta(minutes=5)).isoformat(),first_eligible_at=first.isoformat(),expires_at=(first+timedelta(seconds=30)).isoformat(),decision_at=first.isoformat(),entry_permission=True,permission_policy=policy,permission_eob=first.isoformat(),permission_direction=direction,reference_opponent_price=101. if long else 100.)
        args['entry_policy_proof']=dict(policy='PAPER_ENTRY_V5',path='B',evidence_id='E',allowed=True,reasons=[],eob=first.isoformat(),close_side='above' if long else 'below',slope='up' if long else 'down',hist=1 if long else -1,opposite_divergence=False,closed_h1_known=True,calendar_qualified=True,h1_absolute=direction+'_strong',b_h1_exception=False,m5_reverse_divergence=False,h1_source=dict(closed=True,calendar_qualified=True,entry_eob=(first-timedelta(hours=1)).isoformat()),b_process=dict(process_id='DCE.m2701|'+direction+'|'+start,start_bar_id=start,start_eob=(first-timedelta(minutes=15)).isoformat(),confirmation_bar_id=confirm,confirmation_eob=(first-timedelta(minutes=5)).isoformat(),last_m5_eob=first.isoformat(),status='CONFIRMED_ACTIVE',session_id='SYNTHETIC',age=3))
        b=args['entry_policy_proof']['b_process']
        evidence='B|'+direction+'|'+b['process_id']+'|'+confirm
        args['core_evidence_id']=evidence;args['entry_policy_proof']['evidence_id']=evidence
        req=dict(id='SYNTHETIC_R8',op='submit',account_id='WRONG' if problem=='account' else account,args=args)
        saved=bridge.ROOT;stdin=sys.stdin;out=io.StringIO()
        try:
            bridge.ROOT=root;sys.stdin=io.StringIO(json.dumps(req)+'\n')
            with contextlib.redirect_stdout(out):bridge._serve(config)
        finally:bridge.ROOT=saved;sys.stdin=stdin
        result=json.loads(out.getvalue().splitlines()[-1]);expected=problem is None
        assert result['ok']==expected and len(calls)==int(expected),(problem,direction,policy,result)
        return dict(case=problem or 'PASS_TO_FAKE_NATIVE',direction=direction,policy=policy,ok=result['ok'],fake_native_calls=len(calls),native_submit_invoked=result.get('native_submit_invoked'),reason=result.get('reason'))


if __name__=='__main__':
    rows=[exercise(p,d) for d in ('bull','bear') for p in (None,'pause','expiry','stale_quote','active','account')]
    rows += [exercise(None,d,'CLOSED_FACTS_PERMISSION_V2_20261111') for d in ('bull','bear')]
    print(json.dumps(dict(python=sys.version,passed=len(rows),results=rows,real_sdk_imported=False,real_orders_sent=0,unique_controller='NOT_CREATED_SYNTHETIC_VENDOR_ONLY')))
