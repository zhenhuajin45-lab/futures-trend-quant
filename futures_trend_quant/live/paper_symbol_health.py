"""Request-scoped market admission. Account protection remains global."""
from datetime import datetime,timezone
from pathlib import Path
import math
from .paper_io import read_json

POLICY='SYMBOL_MARKET_ISOLATION_V1_20261111'
SERVICE_POLICY='ACCOUNT_AND_SYMBOL_SERVICE_V2_20261111'


def enabled(c):return c.get('symbol_market_policy')==POLICY


def service_policy(c):
    from .paper_service_health import POLICY as LEGACY
    return SERVICE_POLICY if enabled(c) else LEGACY


def current_m15_event(status,cs,eob,now=None):
    if not enabled(status):return True
    from .paper_tq import clock
    try:
        if now is not None and clock(eob).timestamp()!=int(clock(now).timestamp())//900*900:return False
        return clock(eob)==clock(status['products'][cs]['core']['last_closed_m15'])
    except (ValueError,KeyError,TypeError):return False


def symbol_status(r,symbol,now):
    from .paper_products import product_of
    from .paper_tq import external_product,current_feed_proof
    cs=product_of(symbol);p=r.products.get(cs,{})
    result=dict(policy=POLICY,symbol=symbol,csymbol=cs,checked_at=now.isoformat(),ready=False)
    def fail(reason,**detail):return dict(result,reason=reason,**detail)
    if symbol not in r.records or (p.get('metadata') or {}).get('symbol')!=symbol:return fail('SYMBOL_NOT_REGISTERED')
    if not p.get('ready') or p.get('frozen') or p.get('fault'):return fail('PRODUCT_NOT_READY',fault=p.get('fault'))
    for key in ('market','core'):
        process=getattr(p.get(key),'thread',None)
        if process is not None and not process.is_alive():return fail(key.upper()+'_WORKER_EXITED')
    if external_product(r.c,cs):
        feed=getattr(r,'tq_feed',None)
        if feed is None or not feed.is_alive():return fail('TQ_WORKER_NOT_ALIVE')
        try:
            # Read-completion time, not an earlier coordinator cycle clock.
            proof=current_feed_proof(r.c.get('root','.'),r.c,r.c['config_sha256'],datetime.now(timezone.utc))
            if proof.get('run_id')!=r.run_id:return fail('TQ_RUN_MISMATCH')
            if proof.get('product_status',{}).get(cs,{}).get('ready') is not True:return fail('TQ_SYMBOL_SOURCE_NOT_READY',source_status=proof.get('product_status',{}).get(cs))
            if proof.get('contracts',{}).get(cs)!=symbol or proof.get('main_mapping',{}).get(cs)!=symbol:return fail('TQ_SYMBOL_MAPPING')
            result['source_run_id']=proof['run_id'];result['source_updated_at']=proof['updated_at']
        except (OSError,ValueError,KeyError,TypeError) as exc:return fail('TQ_PROOF_REJECTED',diagnostic=getattr(exc,'details',None) or dict(category=type(exc).__name__,detail=str(exc)[:160]))
    else:
        if getattr(r,'platform_connections',{}).get('market',{}).get('status')!='CONNECTED':return fail('GM_MARKET_DISCONNECTED')
        if symbol not in getattr(r,'tick_subscriptions',set()):return fail('GM_TICK_NOT_SUBSCRIBED')
        from .paper_closed_m1 import enabled as gm_closed
        if gm_closed(r.c) and symbol not in getattr(r,'m1_subscriptions',set()):return fail('GM_M1_NOT_SUBSCRIBED')
    return dict(result,ready=True,reason=None)


def display_status(root,cs,now,status=None,bar_eob=None):
    """No stale historical direction is promoted into a live signal card."""
    from .paper_tq import external_product,final_source,clock
    root=Path(root);profile=root/'config/paper_observation_five.local.json'
    if not profile.exists():
        return dict(ready=not enabled(status or {}),reason='CONFIG_UNKNOWN' if enabled(status or {}) else None,policy='LEGACY_DISPLAY')
    try:c=read_json(profile)
    except (OSError,ValueError,KeyError,TypeError):return dict(ready=False,reason='CONFIG_UNKNOWN')
    if not enabled(c):return dict(ready=True,reason=None,policy='LEGACY_DISPLAY')
    try:
        if bar_eob is not None and clock(bar_eob).timestamp()!=int(clock(now).timestamp())//900*900:
            return dict(ready=False,reason='HISTORICAL_BAR_NOT_CURRENT_SIGNAL')
        s=status or read_json(root/'evidence/paper_observation_five/status.json');p=s['products'][cs];symbol=p['symbol']
        if not 0<=(clock(now)-clock(s['at'])).total_seconds()<=20:return dict(ready=False,reason='STATUS_STALE')
        if not p.get('ready') or p.get('frozen') or p.get('fault'):return dict(ready=False,reason='PRODUCT_NOT_READY')
        registry=read_json(root/'state/paper_observation_five/market_registry.local.json')['contracts'][symbol]
        if not any(a<=clock(now).timestamp()<b for a,b in registry['trade_windows']):return dict(ready=False,reason='CLOSED_SESSION')
        row=read_json(root/'evidence/paper_observation_five/latest_quotes'/(symbol+'.json'));q=row['quote']
        if row.get('config_sha256')!=s['config_sha256'] or row.get('symbol')!=symbol:return dict(ready=False,reason='QUOTE_IDENTITY')
        if any(not 0<=(clock(now)-clock(value)).total_seconds()<=3 for value in (row['received_at'],q['created_at'])):return dict(ready=False,reason='NO_FRESH_MARKET')
        if not math.isfinite(float(q['last'])) or float(q['last'])<=0:return dict(ready=False,reason='NO_VALID_PRICE')
        if external_product(c,cs):final_source(root,c,s['config_sha256'],symbol,q,clock(now),opening=True)
        elif q.get('source') not in ('GM_HOST_TICK_CALLBACK','GM_BULK_CURRENT_POLL'):return dict(ready=False,reason='QUOTE_SOURCE')
        if not external_product(c,cs) and row.get('host_run_id')!=read_json(root/'evidence/paper_observation_five/run_manifest.json')['run_id']:return dict(ready=False,reason='QUOTE_HOST_GENERATION')
        return dict(ready=True,reason=None,policy=POLICY,quote_quality=s.get('quote_eligibility',{}).get(symbol))
    except (OSError,ValueError,KeyError,TypeError) as exc:return dict(ready=False,reason='MARKET_PROOF_UNKNOWN',category=type(exc).__name__)
