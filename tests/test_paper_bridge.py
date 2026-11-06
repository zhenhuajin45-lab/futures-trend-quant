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


def invoke(tmp_path,monkeypatch,requests,fail_cash=False,clock=None,profile_extra=None,positions=None,contracts=None,control=None,before_serve=None,cash_hook=None):
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
        if cash_hook:cash_hook()
        if fail_cash:trade.c_status_fail(501,'cash')
        return dict(account_id=account,nav=1000000.,available=1000000.)
    api.get_cash=cash
    def read(name,value):
        def f(account=None):calls.append((name,account));return value
        return f
    api.get_positions=read('positions',positions or []);api.get_orders=read('orders',[])
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
    config=tmp_path/'config.json';profile=dict(endpoint='api.myquant.cn:9000',live_orders_enabled=False,
        paper_orders_enabled=True,transport='OFFICIAL_SIM_GMTRADE',account_binding='account.json',account_binding_sha256=digest(proof))
    profile.update(profile_extra or {});config.write_text(json.dumps(profile))
    if contracts is not None:(tmp_path/'market.json').write_text(json.dumps(dict(config_sha256=digest(config),contracts=contracts)))
    if control is not None:
        (tmp_path/'state').mkdir();(tmp_path/'state/control.json').write_text(json.dumps(control))
    if before_serve:before_serve(tmp_path,config)
    output=io.StringIO();monkeypatch.setattr(sys,'stdout',output)
    monkeypatch.setattr(sys,'stdin',io.StringIO('\n'.join(json.dumps(x) for x in requests)+'\n'))
    bridge.serve(config)
    return [json.loads(line) for line in output.getvalue().splitlines()],calls


def args(now=None):
    now=now or datetime.now(timezone.utc)
    return dict(symbol='DCE.m2701',volume=1,side=1,position_effect=1,price=101.,stop=97.,multiplier=10.,margin_ratio=.07,
        anchor=99.,anchor_bar_id='DCE.m2701|900s|'+(now-timedelta(minutes=30)).isoformat(),anchor_confirmed_at=(now-timedelta(minutes=15)).isoformat(),
        anchor_episode_id='SYNTHETIC_EPISODE',trend_episode_id='SYNTHETIC_EPISODE',anchor_validated=True,signal_at=now.isoformat(),
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


def multi_profile():
    from futures_trend_quant.live.paper_products import FIVE_POOL
    return dict(schema_version=2,universe=list(FIVE_POOL),market_registry='market.json',state_dir='state',max_positions=2)


def record(cs,tick=1,multiplier=10,margin=.07):
    now=datetime.now(timezone.utc).timestamp()
    return dict(csymbol=cs,tick=tick,multiplier=multiplier,margin_ratio=margin,trade_windows=[(now-3600,now+3600)],entry_windows=[(now-3600,now+3600)])


def test_multi_native_exit_allows_two_products_but_never_a_third_slot(tmp_path,monkeypatch):
    contracts={'DCE.m2701':record('DCE.M'),'SHFE.au2612':record('SHFE.AU',.02,1000,.16),'DCE.eg2701':record('DCE.EG')}
    a=args();b=dict(a,symbol='SHFE.au2612',multiplier=1000,margin_ratio=.16,tick=.02,price=100.02,quote_bid=100,quote_ask=100.02,stop=99.72)
    b['anchor_bar_id']=b['anchor_bar_id'].replace('DCE.m2701','SHFE.au2612')
    c=dict(a,symbol='DCE.eg2701',anchor_bar_id=a['anchor_bar_id'].replace('DCE.m2701','DCE.eg2701'))
    result,calls=invoke(tmp_path,monkeypatch,[req('submit','M',arguments=a),req('submit','AU',arguments=b),req('submit','EG',arguments=c)],profile_extra=multi_profile(),contracts=contracts)
    assert result[0]['ok'] and result[1]['ok'] and not result[2]['ok']
    assert len([x for x in calls if x[0]=='submit'])==2


@pytest.mark.parametrize('effect,today,expected',[(3,1,True),(4,0,True),(4,1,False),(2,1,False)])
def test_actual_bridge_shfe_close_bucket_reaches_correct_native_effect(tmp_path,monkeypatch,effect,today,expected):
    a=dict(args(),symbol='SHFE.au2612',side=2,position_effect=effect,price=100,multiplier=1000,margin_ratio=.16,tick=.02,quote_bid=100,quote_ask=100.02)
    p=SimpleNamespace(account_id=ACCOUNT,symbol='SHFE.au2612',side=1,volume=1,available=1,available_today=today,volume_today=today)
    result,calls=invoke(tmp_path,monkeypatch,[req('submit',arguments=a)],profile_extra=multi_profile(),positions=[p],
        contracts={'SHFE.au2612':record('SHFE.AU',.02,1000,.16)})
    assert result[0]['ok']==expected
    submits=[x[1] for x in calls if x[0]=='submit']
    assert (submits[0]['position_effect']==effect) if expected else not submits


def test_operator_stop_and_unregistered_metadata_stop_at_final_native_call(tmp_path,monkeypatch):
    result,calls=invoke(tmp_path,monkeypatch,[req('submit',arguments=args())],profile_extra=multi_profile(),
        contracts={'DCE.m2701':record('DCE.M')},control=dict(stop_new_open=True))
    assert not result[0]['ok'] and not any(x[0]=='submit' for x in calls)
