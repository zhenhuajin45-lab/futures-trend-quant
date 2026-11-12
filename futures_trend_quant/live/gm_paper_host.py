"""Official GM init/timer lifecycle, with the existing account-scoped SIM bridge.

MODE_LIVE means a realtime event loop, never permission for a real account.
No default-account trade API is called by this module.
"""
from pathlib import Path
import argparse,hashlib,json,os,sys,time,uuid,queue,subprocess
from datetime import datetime,timezone
import pandas as pd
from gm.api import MODE_LIVE,timer,timer_stop,subscribe
from .paper_config import ROOT,load_paper_config
from .paper_broker import atomic
from .paper_five import FiveRunner

SHUTDOWN_POLICY='HOST_SHUTDOWN_FACTS_V2_20261112'


def snapshot_notifier_stop(r,notifier):
    """Freeze the actual owned work proof before termination, without editing clocks."""
    from .paper_io import shared_open
    from .paper_progress import progress_problem
    path=r.evidence/'trend_reminder_health.local.json'
    for attempt in range(3):
        try:
            with shared_open(path) as stream:raw=stream.read()
            break
        except OSError as exc:
            if attempt==2 or not (isinstance(exc,(FileNotFoundError,PermissionError)) or getattr(exc,'winerror',None) in (2,5,32,33)):raise
            time.sleep(.005*(attempt+1))
    body=json.loads(raw.decode('utf-8-sig'));checked=datetime.now(timezone.utc)
    if progress_problem(body,notifier.pid,checked):raise ValueError('NOTIFIER_STOP_WORK_NOT_HEALTHY')
    folder=r.evidence/'shutdown_proofs';folder.mkdir(exist_ok=True)
    target=folder/(r.run_id+'_'+str(notifier.pid)+'.json')
    with target.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    return dict(policy=SHUTDOWN_POLICY,file=target.relative_to(r.evidence).as_posix(),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),
        checked_at=checked.isoformat(),run_id=r.run_id,config_sha256=r.c['config_sha256'],notifier_pid=notifier.pid)


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
    print('[FTQ_PAPER] GM init complete; SIM only; '+str(len(c.get('universe',range(5))))+' products; account-scoped official SIM API.',flush=True)


def on_timer(context):
    r=getattr(context,'ftq_paper_runner',None)
    if r is None or getattr(context,'ftq_paper_stopped',False) or context.ftq_paper_in_cycle:return
    context.ftq_paper_in_cycle=True
    try:
        if getattr(r,'c',{}).get('quote_policy_version')=='GM_HOST_TICK_PRIMARY_V1_20261109':
            targets=set(r.records)-r.tick_subscription_attempted
            from .paper_tq import external_product
            from .paper_products import product_of
            targets={s for s in targets if not external_product(r.c,product_of(s))}
            if targets:
                r.tick_subscription_attempted.update(targets)
                try:
                    subscribe(symbols=','.join(sorted(targets)),frequency='tick',count=0,unsubscribe_previous=False)
                    r.tick_subscriptions.update(targets)
                    r.event('GM_HOST_TICK_SUBSCRIBED',symbols=sorted(targets),frequency='tick')
                except Exception as exc:
                    from .paper_symbol_health import enabled as isolated
                    if r.engine and not isolated(r.c):r.engine.pause_entries('GM_TICK_SUBSCRIPTION_FAILED',source='GM_HOST')
                    r.event('GM_HOST_TICK_SUBSCRIPTION_FAILED',category=type(exc).__name__)
                    r.notice('tick-subscribe:'+r.run_id,'Tick订阅失败·停止新开',['保留原行情与日志；不自动重试订阅或解除保护。'])
        from .paper_closed_m1 import enabled as gm_closed
        if gm_closed(getattr(r,'c',{})):
            from .paper_tq import external_product
            from .paper_products import product_of
            targets={s for s in r.records if not external_product(r.c,product_of(s))}-r.m1_subscription_attempted
            if targets:
                r.m1_subscription_attempted.update(targets)
                try:
                    subscribe(symbols=','.join(sorted(targets)),frequency='60s',count=0,wait_group=False,unsubscribe_previous=False)
                    r.m1_subscriptions.update(targets)
                    r.event('GM_HOST_CLOSED_M1_SUBSCRIBED',symbols=sorted(targets),frequency='60s',wait_group=False)
                except Exception as exc:
                    from .paper_symbol_health import enabled as isolated
                    if r.engine and not isolated(r.c):r.engine.pause_entries('GM_CLOSED_M1_SUBSCRIPTION_FAILED',source='GM_HOST')
                    r.event('GM_CLOSED_M1_SUBSCRIPTION_FAILED',category=type(exc).__name__)
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
    from .paper_symbol_health import enabled as isolated
    if monitor and isolated(r.c) and text=='1202':
        # This callback carries no trustworthy instrument identity. Revoke
        # only GM subscription proofs; attempted sets prevent an auto-retry.
        # A connected callback alone cannot recreate successful subscriptions.
        r.tick_subscriptions.clear();r.m1_subscriptions.clear()
        monitor.fault('market','SUBSCRIPTION_FAILED',now=datetime.now(timezone.utc),code=text);return
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
        state=getattr(r,'tick_rejection_summary',dict(total=0,since_last=0,last_log=None))
        state['total']+=1;state['since_last']+=1;r.tick_rejection_summary=state
        if hasattr(r,'quote_feed_counts'):r.quote_feed_counts['GM_HOST_TICK_REJECTED']+=1
        mono=time.monotonic()
        if state['last_log'] is None or mono-state['last_log']>=30:
            r.event('GM_HOST_TICK_REJECTED',category=type(exc).__name__,
                code=getattr(exc,'code','TICK_ADAPTER_EXCEPTION'),payload_type=type(tick).__name__,
                rejected_since_last=state['since_last'],rejected_total=state['total'])
            state.update(last_log=mono,since_last=0)


def on_bar(context,bars):
    r=getattr(context,'ftq_paper_runner',None)
    if r is None or getattr(context,'ftq_paper_stopped',False):return
    from .paper_closed_m1 import enabled
    if not enabled(r.c):return
    try:r.queue_host_bar(bars,pd.Timestamp.now(tz='Asia/Shanghai'))
    except (ValueError,KeyError,TypeError,queue.Full) as exc:
        from .paper_symbol_health import enabled as isolated
        if r.engine and not isolated(r.c):r.engine.pause_entries('GM_CLOSED_M1_CALLBACK_INVALID',source='GM_HOST')
        r.event('GM_CLOSED_M1_CALLBACK_INVALID',category=type(exc).__name__,detail=str(exc)[:180])


def on_shutdown(context):
    r=getattr(context,'ftq_paper_runner',None)
    if r is None or getattr(context,'ftq_paper_stopped',False):return
    context.ftq_paper_stopped=True
    if getattr(context,'ftq_paper_timer_id',None) is not None:timer_stop(context.ftq_paper_timer_id)
    requested_at=datetime.now(timezone.utc).isoformat();flat=False
    if r.engine:
        r.engine.pause_entries('GM_HOST_SHUTDOWN',source='GM_HOST')
        flat=bool(r.engine.ready and r.engine.last_snapshot and
            (pd.Timestamp.now(tz='Asia/Shanghai')-pd.Timestamp(r.engine.last_snapshot)).total_seconds()<=10 and
            not any(int(p['volume'])>0 for p in r.engine.positions()) and not r.engine.active())
    r.event('GM_PLATFORM_HOST_STOPPING',flat_confirmed=flat,readonly=r.readonly,shutdown_policy=SHUTDOWN_POLICY,
        shutdown_requested_at=requested_at,note='SHUTDOWN_REQUEST_IS_NOT_COMPLETED_WORKER_EXIT_OR_A_CLOSE_FILL')
    if not r.readonly and not flat:
        r.notice('host-stop:'+r.run_id,'平台策略停止仍需核仓',['停止策略不等于已平仓；有余仓或对账未知时，必须在模拟柜台继续处理。'])
    # End the owned notifier before slow market/transport teardown. Its final
    # actual work proof must precede the final STOPPED receipt; never backdate it.
    notifier=getattr(r,'trend_notifier',None)
    notifier_stop_proof=None
    if notifier is not None and notifier.poll() is None:
        try:notifier_stop_proof=snapshot_notifier_stop(r,notifier)
        except Exception as exc:r.event('GM_STOP_NOTIFIER_PROOF_UNAVAILABLE',category=type(exc).__name__,diagnostic=getattr(exc,'details',None))
        notifier.terminate()
        wait=getattr(notifier,'wait',None)
        if callable(wait):
            try:wait(timeout=2)
            except subprocess.TimeoutExpired:pass
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
    if getattr(r,'tq_feed',None) is not None:processes.append(r.tq_feed)
    if getattr(r,'clock_worker',None) is not None:processes.append(r.clock_worker)
    if r.notifications and r.notifications.process:processes.append(r.notifications.process)
    for p in processes:
        if p.is_alive():p.terminate();p.join(timeout=.2)
    if callable(getattr(r.transport,'shutdown',None)):r.transport.shutdown()
    else:
        r.transport.closed=True
        if r.transport.process.poll() is None:r.transport.process.terminate()
    notifier_exited=None if notifier is None else notifier.poll() is not None
    remaining=sum(bool(p.is_alive()) for p in processes)
    # This is the completion boundary, not the beginning of on_shutdown.
    # External stopped/account checks remain mandatory before any recovery.
    r.event('GM_PLATFORM_HOST_STOPPED',flat_confirmed=flat,readonly=r.readonly,shutdown_policy=SHUTDOWN_POLICY,
        shutdown_requested_at=requested_at,notifier_exited=notifier_exited,remaining_owned_workers=remaining,notifier_stop_proof=notifier_stop_proof,
        note='CALLBACK_TEARDOWN_COMPLETED; RECHECK_ALL_OWNED_PROCESSES_AND_FRESH_BROKER_FACTS; NO_ASSUMED_CLOSE_FILL')
    print('[FTQ_PAPER] Host stopped; broker flat confirmed='+str(flat)+'; never assume a close fill.',flush=True)
