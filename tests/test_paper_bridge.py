"""Exercise the real isolated worker with a fake vendor API and stdin protocol."""
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace,ModuleType
import io
import json
import sys
import pytest
from test_paper_execution import ACCOUNT
from futures_trend_quant.live import paper_sim_bridge as bridge
from futures_trend_quant.live.paper_config import digest


def invoke(tmp_path,monkeypatch,requests,fail_cash=False,clock=None):
    calls=[];ctx=SimpleNamespace(logined_accounts={});active=[]
    api=ModuleType('gmtrade.api');trade=ModuleType('gmtrade.api.trade')
    storage=ModuleType('gmtrade.api.storage');storage.ctx=ctx
    trade.c_status_fail=lambda status,name:status!=0
    api.trade=trade
    def token(x):assert x=='SYNTHETIC_ONLY';calls.append(('token',None))
    api.set_token=token;api.set_endpoint=lambda x:calls.append(('endpoint',x))
    api.account=lambda account_id:SimpleNamespace(account_id=account_id)
    def login(a):ctx.logined_accounts[a.account_id]=a
    api.login=login
    def cash(account=None):
        calls.append(('cash',account))
        if fail_cash:trade.c_status_fail(501,'cash')
        return dict(account_id=account,nav=1000000.,available=1000000.)
    api.get_cash=cash
    def read(name,value):
        def f(account=None):calls.append((name,account));return value
        return f
    api.get_positions=read('positions',[]);api.get_orders=read('orders',[])
    api.get_unfinished_orders=read('active',active);api.get_execution_reports=read('fills',[])
    def order(**kwargs):
        calls.append(('submit',kwargs))
        row=dict(account_id=kwargs['account'],cl_ord_id='ORDER1',symbol=kwargs['symbol'],volume=kwargs['volume'])
        active.append(SimpleNamespace(**row));return [row]
    api.order_volume=order;api.order_cancel=lambda rows:calls.append(('cancel',rows))
    parent=ModuleType('gmtrade');parent.api=api
    for k,v in [('gmtrade',parent),('gmtrade.api',api),('gmtrade.api.trade',trade),('gmtrade.api.storage',storage)]:monkeypatch.setitem(sys.modules,k,v)
    import google.protobuf.json_format as j
    monkeypatch.setattr(j,'MessageToDict',lambda x,**kwargs:dict(x) if isinstance(x,dict) else vars(x))
    monkeypatch.setenv('GM_TOKEN','SYNTHETIC_ONLY');monkeypatch.setattr(bridge,'ROOT',tmp_path)
    if clock is not None:monkeypatch.setattr(bridge,'datetime',clock)
    proof=tmp_path/'account.json';proof.write_text(json.dumps(dict(account_id=ACCOUNT,endpoint='api.myquant.cn:9000',
        source='GM_OFFICIAL_SIM_ACCOUNT_CASH_ID_MATCH',user_designated_futures_sim=True)))
    config=tmp_path/'config.json';config.write_text(json.dumps(dict(endpoint='api.myquant.cn:9000',live_orders_enabled=False,
        paper_orders_enabled=True,transport='OFFICIAL_SIM_GMTRADE',account_binding='account.json',account_binding_sha256=digest(proof))))
    output=io.StringIO();monkeypatch.setattr(sys,'stdout',output)
    monkeypatch.setattr(sys,'stdin',io.StringIO('\n'.join(json.dumps(x) for x in requests)+'\n'))
    bridge.serve(config)
    return [json.loads(line) for line in output.getvalue().splitlines()],calls


def args(now=None):
    now=now or datetime.now(timezone.utc)
    return dict(symbol='DCE.m2701',volume=1,side=1,position_effect=1,price=101.,stop=97.,multiplier=10.,margin_ratio=.07,
        quote_event_at=now.isoformat(),quote_received_at=now.isoformat(),quote_bid=100.,quote_ask=101.,tick=1.,day_nav=1000000.,peak_nav=1000000.)


def req(op,rid='R1',account=ACCOUNT,arguments=None):return dict(id=rid,op=op,account_id=account,args=arguments or {})


def test_real_bridge_queries_submits_cancels_all_explicitly_bound_and_deduplicates(tmp_path,monkeypatch):
    submit=req('submit','S1',arguments=args())
    result,calls=invoke(tmp_path,monkeypatch,[req('snapshot'),submit,submit,req('cancel','C1',arguments=dict(cl_ord_id='ORDER1'))])
    assert all(x['ok'] for x in result) and len([x for x in calls if x[0]=='submit'])==1
    for name,value in calls:
        if name in ('cash','positions','orders','active','fills'):assert value==ACCOUNT
        if name=='submit':assert value['account']==ACCOUNT and value['order_type']==1 and value['volume']==1
        if name=='cancel':assert value==[dict(cl_ord_id='ORDER1',account_id=ACCOUNT)]
    assert ('endpoint','api.myquant.cn:9000') in calls


@pytest.mark.parametrize('account',[None,'','OTHER_ACCOUNT'])
def test_missing_default_or_wrong_account_never_reaches_vendor_orders(tmp_path,monkeypatch,account):
    result,calls=invoke(tmp_path,monkeypatch,[req('submit',account=account,arguments=args())])
    assert not result[0]['ok'] and not any(x[0]=='submit' for x in calls)


def test_vendor_empty_on_failure_cannot_be_misread_as_empty_account(tmp_path,monkeypatch):
    result,calls=invoke(tmp_path,monkeypatch,[req('snapshot')],fail_cash=True)
    assert not result[0]['ok'] and result[0]['reason']=='SIM_RPC_STATUS_501'


@pytest.mark.parametrize('patch',[{'volume':2},{'symbol':'SHSE.600000'},{'price':0},{'price':105},{'stop':-1},{'stop':1,'multiplier':100},{'margin_ratio':100},{'multiplier':float('nan')}])
def test_final_native_submit_risk_price_instrument_guards(tmp_path,monkeypatch,patch):
    a=args();a.update(patch)
    result,calls=invoke(tmp_path,monkeypatch,[req('submit',arguments=a)])
    assert not result[0]['ok'] and result[0]['submit_may_have_reached_broker'] is False
    assert not any(x[0]=='submit' for x in calls)


def test_quote_that_ages_during_snapshot_is_rejected_at_final_exit(tmp_path,monkeypatch):
    base=datetime.now(timezone.utc);ticks=iter([base,base+timedelta(seconds=4)])
    class Clock:
        fromisoformat=staticmethod(datetime.fromisoformat)
        @staticmethod
        def now(tz):return next(ticks)
    result,calls=invoke(tmp_path,monkeypatch,[req('submit',arguments=args(base))],clock=Clock)
    assert not result[0]['ok'] and not any(x[0]=='submit' for x in calls)
