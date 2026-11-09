"""Official GM init/timer lifecycle, with the existing account-scoped SIM bridge.

MODE_LIVE means a realtime event loop, never permission for a real account.
No default-account trade API is called by this module.
"""
from pathlib import Path
import argparse,hashlib,json,os,sys,time,uuid
from datetime import datetime,timezone
import pandas as pd
from gm.api import MODE_LIVE,timer,timer_stop,subscribe
from .paper_config import ROOT,load_paper_config
from .paper_broker import atomic
from .paper_five import FiveRunner


def launch_parameters(argv,project_root,registration_path):
    """Honor the client's standard arguments; no token/id literals in public code."""
    p=argparse.ArgumentParser(add_help=False);p.add_argument('--strategy_id');p.add_argument('--token')
    args,_=p.parse_known_args(argv)
    path=Path(registration_path)
    registration=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    strategy_id=args.strategy_id or os.environ.get('GM_STRATEGY_ID') or registration.get('strategy_id','')
    uuid.UUID(strategy_id)  # Never invent a strategy id or silently attach an arbitrary strategy.
    token=args.token or os.environ.get('GM_TOKEN','')
    auth=Path(project_root)/'config/paper_platform_auth.local.json'
    if not token and auth.exists():token=json.loads(auth.read_text(encoding='utf-8-sig')).get('GM_TOKEN','')
    if not isinstance(token,str) or not token:raise ValueError('GM_TOKEN missing from client arguments/environment/private local configuration')
    return strategy_id,token


def init(context):
    if context.mode!=MODE_LIVE:raise ValueError('paper host requires GM realtime MODE_LIVE, not backtest')
    if getattr(context,'ftq_paper_runner',None) is not None:raise ValueError('duplicate host initialization')
    path=Path(os.environ.get('GM_PAPER_CONFIG',str(ROOT/'config/paper_observation_five.local.json'))).resolve()
    c=load_paper_config(path)
    if c['schema_version']!=2:raise ValueError('five-product paper profile required')
    if not os.environ.get('GM_TOKEN'):raise ValueError('GM_TOKEN must be configured locally before starting the client')
    readonly_flag=os.environ.get('GM_PAPER_HOST_READONLY','0')
    if readonly_flag not in ('0','1'):raise ValueError('GM_PAPER_HOST_READONLY must be exactly 0 or 1')
    readonly=readonly_flag=='1'
    # The same account mutex rejects an already-running CLI controller BEFORE creating a bridge.
    started=time.monotonic();r=FiveRunner(path,readonly=readonly);context.ftq_paper_runner=r
    r.platform_connections={k:dict(v,run_id=r.run_id) for k,v in getattr(context,'ftq_paper_connections',{}).items()}
    for service,state in r.platform_connections.items():r.event('GM_CONNECTION_STATUS',service=service,pre_init=True,**state)
    context.ftq_paper_in_cycle=False;context.ftq_paper_stopped=False
    context.ftq_paper_initialization_seconds=time.monotonic()-started
    r.last_loop=time.monotonic()  # Same initialization/running boundary as the CLI run().
    result=timer(timer_func=on_timer,period=200,start_delay=200)
    if result.get('status',result.get('timer_status'))!=0:
        on_shutdown(context);raise RuntimeError('GM_TIMER_REGISTRATION_FAILED')
    context.ftq_paper_timer_id=result['timer_id']
    r.event('GM_PLATFORM_HOST_STARTED',mode='MODE_LIVE_SIM_ACCOUNT_ONLY',readonly=readonly,
        timer_period_ms=200,trade_transport='OFFICIAL_SIM_GMTRADE_EXPLICIT_ACCOUNT',default_account_trade_api=False)
    atomic(r.evidence/'gm_platform_host.local.json',dict(pid=os.getpid(),at=str(pd.Timestamp.now(tz='Asia/Shanghai')),
        driver='GM_INIT_TIMER',timer_period_ms=200,readonly=readonly,config_sha256=c['config_sha256'],live_orders_enabled=False,
        initialization_before_timer_seconds=context.ftq_paper_initialization_seconds,
        host_sha256_lf=hashlib.sha256(Path(__file__).read_bytes().replace(b'\r\n',b'\n')).hexdigest(),
        entry_sha256_lf=hashlib.sha256(Path(sys.modules['__main__'].__file__).read_bytes().replace(b'\r\n',b'\n')).hexdigest()))
    print('[FTQ_PAPER] GM init complete; SIM only; five products; account-scoped official SIM API.',flush=True)


def on_timer(context):
    r=getattr(context,'ftq_paper_runner',None)
    if r is None or getattr(context,'ftq_paper_stopped',False) or context.ftq_paper_in_cycle:return
    context.ftq_paper_in_cycle=True
    try:
        if getattr(r,'c',{}).get('quote_policy_version')=='GM_HOST_TICK_PRIMARY_V1_20261109':
            targets=set(r.records)-r.tick_subscription_attempted
            if targets:
                r.tick_subscription_attempted.update(targets)
                try:
                    subscribe(symbols=','.join(sorted(targets)),frequency='tick',count=0,unsubscribe_previous=False)
                    r.tick_subscriptions.update(targets)
                    r.event('GM_HOST_TICK_SUBSCRIBED',symbols=sorted(targets),frequency='tick')
                except Exception as exc:
                    if r.engine:r.engine.pause_entries('GM_TICK_SUBSCRIPTION_FAILED',source='GM_HOST')
                    r.event('GM_HOST_TICK_SUBSCRIPTION_FAILED',category=type(exc).__name__)
                    r.notice('tick-subscribe:'+r.run_id,'Tick订阅失败·停止新开',['保留原行情与日志；不自动重试订阅或解除保护。'])
        if r.cycle():
            timer_stop(context.ftq_paper_timer_id);context.ftq_paper_preflight_passed=True
            print('[FTQ_PAPER] READONLY_PREFLIGHT_PASS; no paper orders were sent.',flush=True)
    except Exception as exc:
        if r.engine:
            r.engine.pause_entries('GM_PLATFORM_CYCLE_FAULT',source='GM_HOST');r.engine.ready=False;r.engine.save()
        r.event('GM_PLATFORM_CYCLE_FAULT',category=type(exc).__name__)
        r.notice('host-fault:'+r.run_id,'平台策略执行异常',['新开仓暂停；已知敞口继续监督。',type(exc).__name__])
    finally:context.ftq_paper_in_cycle=False


def on_error(context,code,info):
    text=str(code)
    if text in ('1200','1201'):_connection(context,'market','CONNECT_FAILED' if text=='1200' else 'DISCONNECTED')
    r=getattr(context,'ftq_paper_runner',None)
    if r is None or getattr(context,'ftq_paper_stopped',False):return
    # Vendor text can contain private environment data; record code only.
    r.event('GM_PLATFORM_ERROR',code=str(code))
    monitor=getattr(r,'service_health',None)
    if monitor and text in ('1200','1201'):
        monitor.fault('market','CONNECT_FAILED' if text=='1200' else 'DISCONNECTED',now=datetime.now(timezone.utc),code=text)
        return
    if monitor:
        monitor.fault('platform','RPC_FAILED',now=datetime.now(timezone.utc),code=text)
        return
    if r.engine:r.engine.pause_entries('GM_ERROR_'+text,source='GM_HOST')
    names={'1200':'实时行情服务连接失败','1201':'实时行情服务连接断开','1202':'实时行情订阅失败'}
    r.notice('host-error:'+r.run_id+':'+str(code),'掘金平台错误',[names.get(text,'平台错误')+'（'+text+'）','暂停新开；不改为默认账户，不自动重发不明委托。'],severity='critical')


def _connection(context,service,state):
    if getattr(context,'ftq_paper_stopped',False):return
    records=getattr(context,'ftq_paper_connections',{})
    observed=dict(status=state,observed_at=datetime.now(timezone.utc).isoformat(),scope='GM_HOST_EVENT_LOOP_NOT_SIM_ACCOUNT_RECON')
    records[service]=observed;context.ftq_paper_connections=records
    r=getattr(context,'ftq_paper_runner',None)
    if r is None:return
    if not hasattr(r,'platform_connections'):r.platform_connections={}
    r.platform_connections[service]=dict(observed,run_id=r.run_id)
    r.event('GM_CONNECTION_STATUS',service=service,pre_init=False,**r.platform_connections[service])
    if state!='CONNECTED' and r.engine:
        monitor=getattr(r,'service_health',None)
        if monitor:monitor.fault(service,state,now=observed['observed_at'])
        else:r.engine.pause_entries(service+'_'+state,source='GM_CONNECTION')
    # A connected event is evidence, never authorization to clear a pause/halt.


def on_market_data_connected(context):_connection(context,'market','CONNECTED')
def on_trade_data_connected(context):_connection(context,'host_trade','CONNECTED')
def on_market_data_disconnected(context):_connection(context,'market','DISCONNECTED')
def on_trade_data_disconnected(context):_connection(context,'host_trade','DISCONNECTED')


def on_tick(context,tick):
    r=getattr(context,'ftq_paper_runner',None)
    if r is None or getattr(context,'ftq_paper_stopped',False):return
    if getattr(r,'c',{}).get('quote_policy_version')!='GM_HOST_TICK_PRIMARY_V1_20261109':return
    try:r.queue_host_tick(tick,pd.Timestamp.now(tz='Asia/Shanghai'))
    except (ValueError,KeyError,TypeError) as exc:
        r.event('GM_HOST_TICK_REJECTED',category=type(exc).__name__)


def on_shutdown(context):
    r=getattr(context,'ftq_paper_runner',None)
    if r is None or getattr(context,'ftq_paper_stopped',False):return
    context.ftq_paper_stopped=True
    if getattr(context,'ftq_paper_timer_id',None) is not None:timer_stop(context.ftq_paper_timer_id)
    flat=False
    if r.engine:
        r.engine.pause_entries('GM_HOST_SHUTDOWN',source='GM_HOST')
        flat=bool(r.engine.ready and r.engine.last_snapshot and
            (pd.Timestamp.now(tz='Asia/Shanghai')-pd.Timestamp(r.engine.last_snapshot)).total_seconds()<=10 and
            not any(int(p['volume'])>0 for p in r.engine.positions()) and not r.engine.active())
    r.event('GM_PLATFORM_HOST_STOPPED',flat_confirmed=flat,readonly=r.readonly,
        note='STOPPING_EVENT_LOOP_IS_NOT_A_FILL; ANY_UNCONFIRMED_OR_REMAINING_EXPOSURE_REQUIRES_OPERATOR_MANAGEMENT')
    if not r.readonly and not flat:
        r.notice('host-stop:'+r.run_id,'平台策略停止仍需核仓',['停止策略不等于已平仓；有余仓或对账未知时，必须在模拟柜台继续处理。'])
    # Only workers created by this host are released. No OS process discovery or other strategies.
    # Producers can have a blocked multiprocessing feeder while a warmup child
    # is terminated. Do not join undeliverable data queues during interpreter exit.
    channels=[getattr(r,'quote_in',None),getattr(r,'quote_events',None)]
    for p in r.products.values():
        channels += [getattr(p['market'],'events',None),getattr(p['core'],'inbox',None),getattr(p['core'],'outbox',None)]
    for channel in channels:
        if channel is not None:
            for method in ('cancel_join_thread','close'):
                action=getattr(channel,method,None)
                if callable(action):action()
    processes=[r.quote_worker]+[x['market'].thread for x in r.products.values()]+[x['core'].thread for x in r.products.values()]
    if getattr(r,'clock_worker',None) is not None:processes.append(r.clock_worker)
    if r.notifications and r.notifications.process:processes.append(r.notifications.process)
    for p in processes:
        if p.is_alive():p.terminate();p.join(timeout=.2)
    notifier=getattr(r,'trend_notifier',None)
    if notifier is not None and notifier.poll() is None:notifier.terminate()
    if callable(getattr(r.transport,'shutdown',None)):r.transport.shutdown()
    else:
        r.transport.closed=True
        if r.transport.process.poll() is None:r.transport.process.terminate()
    print('[FTQ_PAPER] Host stopped; broker flat confirmed='+str(flat)+'; never assume a close fill.',flush=True)
