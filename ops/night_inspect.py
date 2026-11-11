"""Local read-only QA. No SDK calls, process control, state or source mutation."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import argparse,json,hashlib

BENIGN_DELIVERY={'SENT','PENDING','SUPERSEDED_NOT_SENT','EXPIRED_NOT_SENT','LOCAL_SOURCE_ONLY'}
TZ=timezone(timedelta(hours=8))


def inspect(client):
    ev=client/'_strategy_runtime/evidence/paper_observation_five';now=datetime.now(timezone.utc)
    read=lambda name:json.loads((ev/name).read_text(encoding='utf-8-sig'))
    s=read('status.json');host=read('gm_platform_host.local.json');broker=s.get('broker') or {};issues=[]
    age=(now-datetime.fromisoformat(s['at'])).total_seconds()
    if not 0<=age<=20:issues.append('STATUS_STALE_OR_FUTURE')
    isolated=s.get('symbol_market_policy')=='SYMBOL_MARKET_ISOLATION_V1_20261111'
    if s.get('ready_products')!=len(s.get('universe') or s.get('products') or range(5)) and not isolated:issues.append('PRODUCTS_NOT_ALL_READY')
    if broker.get('status')!='RECONCILED':issues.append('ACCOUNT_NOT_RECONCILED')
    if broker.get('pause_new'):issues.append('NEW_OPENS_PAUSED')
    if broker.get('halt_reason'):issues.append('RISK_HALT:'+broker['halt_reason'])
    if not s.get('quote_worker_alive'):issues.append('QUOTE_PROCESS_DOWN')
    for cs,p in s.get('products',{}).items():
        if not p.get('market_alive') or not p.get('core_alive'):issues.append('WORKER_DOWN:'+cs)
        if p.get('fault'):issues.append(cs+':'+str(p['fault']))
    notification=s.get('notifications') or {}
    if not notification.get('worker_alive') or not notification.get('health_fresh'):issues.append('NOTIFICATION_HEALTH_NOT_READY')
    for status,n in notification.get('outbox_statuses',{}).items():
        if n and status not in BENIGN_DELIVERY:issues.append('FEISHU_'+status)
    sha=hashlib.sha256((client/'main.py').read_bytes().replace(b'\r\n',b'\n')).hexdigest()
    if sha!=host['entry_sha256_lf']:issues.append('MAIN_FINGERPRINT_MISMATCH')
    prefixes=[]
    targets=set(ev.glob('*.local.log'))|set(ev.glob('*.jsonl'))|{ev/'status.json',ev/'gm_platform_host.local.json',ev/'service_health.local.json',ev/'clock_health.local.json',ev/'fills.csv'}
    external=s.get('external_market_feed') or {}
    if external:
        if not external.get('worker_alive') or external.get('status')!='RUNNING':issues.append('TQ_FEED_NOT_RUNNING')
        run_id=external.get('run_id','')
        if run_id and all(c.isalnum() or c in '_-' for c in run_id):targets.update((ev/'tq_feed'/run_id).glob('*.jsonl'))
        else:issues.append('TQ_FEED_GENERATION_UNKNOWN')
    for p in sorted(targets):
        if not p.exists():continue
        size=p.stat().st_size;count=0;digest=hashlib.sha256()
        with p.open('rb') as f:
            while count<size:
                blob=f.read(min(1048576,size-count))
                if not blob:break
                count+=len(blob);digest.update(blob)
        prefixes.append(dict(file=p.name,bytes_hashed=count,sha256=digest.hexdigest(),hash_scope='EXACT_READ_PREFIX'))
    positive=[p for p in broker.get('positions',[]) if int(p.get('volume',0))>0]
    record=dict(at=now.astimezone(TZ).isoformat(),status_at=s['at'],status_age_seconds=age,controller_pid=host['pid'],
        phase=s.get('phase'),ready_products=s.get('ready_products'),issues=issues,main_sha256_lf=sha,
        broker_status=broker.get('status'),positive_positions=positive,active_intents=broker.get('active_intents'),
        position_plans=broker.get('position_plans'),due_events=broker.get('due_events'),products=s.get('products'),
        notifications=notification,clock_health=s.get('clock_health'),service_health=s.get('service_health'),
        quote_feed=s.get('quote_feed'),quote_eligibility=s.get('quote_eligibility'),original_log_prefix_index=prefixes,
        symbol_market_policy=s.get('symbol_market_policy'),symbol_entry_readiness=s.get('symbol_entry_readiness'),
        safety='NO_TRADE_API_NO_STRATEGY_MUTATION',notification_note='Expected supersession/expiry is not delivery failure; compare genuine failures incrementally.')
    qa=ev/'inspections';qa.mkdir(exist_ok=True);path=qa/(now.astimezone(TZ).strftime('%Y%m%d_%H%M%S_%f')+'_inspection.json')
    path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
    return dict(at=record['at'],phase=record['phase'],ready=record['ready_products'],issues=issues,
        readiness_scope='SHARED_ACCOUNT_AND_SEPARATE_SYMBOLS' if isolated else 'LEGACY_WHOLE_POOL',
        symbol_readiness=s.get('symbol_entry_readiness'),
        positive_positions=len(positive),active_intents=broker.get('active_intents'),receipt=str(path))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--client',type=Path,default=Path(__file__).resolve().parents[1]);a=p.parse_args()
    print(json.dumps(inspect(a.client),ensure_ascii=False))
