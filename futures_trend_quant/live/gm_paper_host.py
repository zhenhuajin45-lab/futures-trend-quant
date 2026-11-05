"""Official GM init/timer lifecycle, with the existing account-scoped SIM bridge.

MODE_LIVE means a realtime event loop, never permission for a real account.
No default-account trade API is called by this module.
"""
from pathlib import Path
import os
import pandas as pd
from gm.api import MODE_LIVE,timer,timer_stop
from .paper_config import ROOT,load_paper_config
from .paper_broker import atomic
from .paper_five import FiveRunner


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
    r=FiveRunner(path,readonly=readonly);context.ftq_paper_runner=r
    context.ftq_paper_in_cycle=False;context.ftq_paper_stopped=False
    result=timer(timer_func=on_timer,period=200,start_delay=200)
    if result.get('status',result.get('timer_status'))!=0:
        on_shutdown(context);raise RuntimeError('GM_TIMER_REGISTRATION_FAILED')
    context.ftq_paper_timer_id=result['timer_id']
    r.event('GM_PLATFORM_HOST_STARTED',mode='MODE_LIVE_SIM_ACCOUNT_ONLY',readonly=readonly,
        timer_period_ms=200,trade_transport='OFFICIAL_SIM_GMTRADE_EXPLICIT_ACCOUNT',default_account_trade_api=False)
    atomic(r.evidence/'gm_platform_host.local.json',dict(pid=os.getpid(),at=str(pd.Timestamp.now(tz='Asia/Shanghai')),
        driver='GM_INIT_TIMER',timer_period_ms=200,readonly=readonly,config_sha256=c['config_sha256'],live_orders_enabled=False))
    print('[FTQ_PAPER] GM init complete; SIM only; five products; account-scoped official SIM API.',flush=True)


def on_timer(context):
    r=getattr(context,'ftq_paper_runner',None)
    if r is None or getattr(context,'ftq_paper_stopped',False) or context.ftq_paper_in_cycle:return
    context.ftq_paper_in_cycle=True
    try:
        if r.cycle():
            timer_stop(context.ftq_paper_timer_id);context.ftq_paper_preflight_passed=True
            print('[FTQ_PAPER] READONLY_PREFLIGHT_PASS; no paper orders were sent.',flush=True)
    except Exception as exc:
        if r.engine:
            r.engine.s['pause_new']=True;r.engine.ready=False;r.engine.save()
        r.event('GM_PLATFORM_CYCLE_FAULT',category=type(exc).__name__)
        r.notice('host-fault:'+r.run_id,'平台策略执行异常',['新开仓暂停；已知敞口继续监督。',type(exc).__name__])
    finally:context.ftq_paper_in_cycle=False


def on_error(context,code,info):
    r=getattr(context,'ftq_paper_runner',None)
    if r is None:return
    # Vendor text can contain private environment data; record code only.
    r.event('GM_PLATFORM_ERROR',code=str(code))
    if r.engine:r.engine.s['pause_new']=True;r.engine.save()
    r.notice('host-error:'+r.run_id+':'+str(code),'掘金平台错误',['错误码 '+str(code),'暂停新开；不改为默认账户，不自动重发不明委托。'])


def on_shutdown(context):
    r=getattr(context,'ftq_paper_runner',None)
    if r is None or getattr(context,'ftq_paper_stopped',False):return
    context.ftq_paper_stopped=True
    if getattr(context,'ftq_paper_timer_id',None) is not None:timer_stop(context.ftq_paper_timer_id)
    flat=False
    if r.engine:
        r.engine.s['pause_new']=True;r.engine.save()
        flat=bool(r.engine.ready and r.engine.last_snapshot and
            (pd.Timestamp.now(tz='Asia/Shanghai')-pd.Timestamp(r.engine.last_snapshot)).total_seconds()<=10 and
            not any(int(p['volume'])>0 for p in r.engine.positions()) and not r.engine.active())
    r.event('GM_PLATFORM_HOST_STOPPED',flat_confirmed=flat,readonly=r.readonly,
        note='STOPPING_EVENT_LOOP_IS_NOT_A_FILL; ANY_UNCONFIRMED_OR_REMAINING_EXPOSURE_REQUIRES_OPERATOR_MANAGEMENT')
    if not r.readonly and not flat:
        r.notice('host-stop:'+r.run_id,'平台策略停止仍需核仓',['停止策略不等于已平仓；有余仓或对账未知时，必须在模拟柜台继续处理。'])
    # Only workers created by this host are released. No OS process discovery or other strategies.
    processes=[r.quote_worker]+[x['market'].thread for x in r.products.values()]+[x['core'].thread for x in r.products.values()]
    if r.notifications and r.notifications.process:processes.append(r.notifications.process)
    for p in processes:
        if p.is_alive():p.terminate();p.join(timeout=.2)
    r.transport.closed=True
    if r.transport.process.poll() is None:r.transport.process.terminate()
    print('[FTQ_PAPER] Host stopped; broker flat confirmed='+str(flat)+'; never assume a close fill.',flush=True)
