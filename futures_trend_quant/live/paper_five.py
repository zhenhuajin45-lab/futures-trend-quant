"""Five-product S0 paper observation. One account engine, independent product facts/clocks."""
from collections import Counter
from pathlib import Path
from datetime import datetime,timezone
import argparse,hashlib,json,multiprocessing,os,queue,subprocess,time
import numpy as np
import pandas as pd
from .paper_config import ROOT,load_paper_config
from .paper_products import FIVE_POOL,PRODUCTS,product_of
from .paper_calendar import PortfolioCalendar,PaperCalendar
from .paper_broker import PaperEngine,atomic,stamp,TERMINAL
from .paper_data import MarketWorker,SignalWorker
from .paper_transport import SimTransport
from .paper_runner import PaperRunner,elapsed_review_slots
from .paper_notify import FeishuOutbox,message
from .paper_explain import context as reason_context,lines as reason_lines,EXITS,DIRECTIONS,closing_gross
from .paper_cards import POLICY as CARD_POLICY,freeze_plan,candidate_card,intent_plan,fill_protection,number,reason_text,time_text,intent_reference
from .paper_io import read_json


def code_revision(root):
    try:return subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True,stderr=subprocess.DEVNULL).strip()
    except (OSError,subprocess.SubprocessError):return 'UNKNOWN; SEE_SOURCE_HASHES'


def contract_record(meta,calendar):
    spec=meta['spec'];values=[float(spec[k]) for k in ('price_tick','multiplier','margin_ratio')]
    if not all(np.isfinite(x) and x>0 for x in values) or values[2]>1:raise ValueError('invalid official contract metadata')
    ns=calendar.ns;cuts=np.r_[0,np.flatnonzero(np.diff(ns)>60_000_000_000)+1,len(ns)]
    trade=[(int(ns[a]-60_000_000_000)/1e9,int(ns[b-1])/1e9) for a,b in zip(cuts[:-1],cuts[1:])]
    p=calendar.policy.products[calendar.cs]
    blocks=list(p['blackouts'])+[(v-300_000_000_000,v) for v in p['day_closes'].values()]
    entry=[]
    for a,b in trade:
        pieces=[(a,b)]
        for x,y in blocks:
            x/=1e9;y/=1e9;updated=[]
            for c,d in pieces:
                if y<=c or x>=d:updated.append((c,d))
                else:
                    if c<x:updated.append((c,min(x,d)))
                    if y<d:updated.append((max(y,c),d))
            pieces=updated
        entry.extend(pieces)
    return dict(csymbol=meta['csymbol'],symbol=meta['symbol'],tick=values[0],multiplier=values[1],margin_ratio=values[2],
        trade_windows=trade,entry_windows=entry,source='GM_ACTUAL_CONTRACT_METADATA_AND_EXCHANGE_CALENDAR')


def quote_process(inbox,events,evidence_dir,config_sha256=None,publish_cache=True,excluded_products=(),latest_run=None):
    from gm.api import set_token,current
    set_token(os.environ['GM_TOKEN']);records={};last=0.;errors=0
    path=Path(evidence_dir)/'quote_seen.jsonl'
    while True:
        try:
            while True:
                records=inbox.get_nowait()
                records={s:r for s,r in records.items() if product_of(s) not in excluded_products}
        except queue.Empty:pass
        now=time.time();active={s:r for s,r in records.items() if any(a<=now<b for a,b in r['trade_windows'])}
        wanted=active if active else records
        interval=1 if active else 30
        if wanted and time.monotonic()-last>=interval:
            try:
                with path.open('a',encoding='utf8') as f:f.write(json.dumps(dict(requested_at=datetime.now(timezone.utc).isoformat(),purpose='PAPER_OBSERVATION_SEEN',symbols=sorted(wanted),source='GM_BULK_CURRENT_NOT_TICK_STREAM'))+'\n')
                rows=current(symbols=','.join(sorted(wanted)));received=pd.Timestamp.now(tz='Asia/Shanghai').isoformat()
                for row in rows or []:
                    symbol=row['symbol']
                    if symbol not in wanted:raise ValueError('foreign quote symbol')
                    book=row.get('quotes') or [];level=book[0] if book else {}
                    q=dict(created_at=str(row['created_at']),last=row.get('price'),tick=wanted[symbol]['tick'],
                        bid=level.get('bid_p',0),ask=level.get('ask_p',0),bid_size=level.get('bid_v',0),ask_size=level.get('ask_v',0),source='GM_BULK_CURRENT_POLL')
                    if config_sha256 and publish_cache:
                        atomic(Path(evidence_dir)/'latest_quotes'/(symbol+'.json'),
                               dict(symbol=symbol,config_sha256=config_sha256,received_at=received,quote=q))
                    if latest_run:
                        atomic(Path(evidence_dir)/'quote_poll_latest'/latest_run/(symbol+'.json'),
                            dict(symbol=symbol,quote=q,received_at=received,host_run_id=latest_run,config_sha256=config_sha256))
                    else:events.put(('QUOTE',symbol,q,received))
                errors=0
            except Exception as exc:
                errors+=1;events.put(('QUOTE_ERROR',type(exc).__name__,errors))
            last=time.monotonic()
        time.sleep(min(10,2**min(errors,3)) if errors else .2 if active else 1)


class FiveRunner:
    _acquire_lock=PaperRunner._acquire_lock
    event=PaperRunner.event
    def __init__(self,path,readonly=False):
        self.config_path=Path(path).resolve();self.c=load_paper_config(path);self.readonly=readonly
        if self.c['schema_version']!=2:raise ValueError('five-product schema required')
        self.started=pd.Timestamp.now(tz='Asia/Shanghai');self.run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        if readonly:
            base=ROOT/'data/cache/five_preflight'/self.run_id;self.c['state_dir']=str(base/'state');self.c['evidence_dir']=str(base/'evidence')
        self.evidence=Path(self.c['evidence_dir']);self.evidence.mkdir(parents=True,exist_ok=True);self._acquire_lock()
        if not readonly and not (Path(self.c['state_dir'])/'migration.local.json').exists():raise ValueError('validated account handoff manifest required')
        self.calendar=PortfolioCalendar();self.products={};self.records={};self.engine=None;self.verified=False;self.readonly_snapshot=None
        self.transport=SimTransport(self.c,self.config_path);ctx=multiprocessing.get_context('spawn')
        self.quote_in=ctx.Queue();self.quote_events=ctx.Queue()
        from .paper_tq import enabled as tq_enabled,TQ_ADDED
        external=TQ_ADDED if tq_enabled(self.c) else ()
        from .paper_gm_handoff import enabled as priority,LatestQuotes
        self.quote_worker=ctx.Process(target=quote_process,args=(self.quote_in,self.quote_events,str(self.evidence),self.c['config_sha256'],not bool(self.c.get('quote_policy_version')),external,self.run_id if priority(self.c) else None),daemon=True,name='paper-five-quotes')
        self.tq_feed=None
        if external:
            from .paper_tq_market import OwnedFeed
            self.tq_feed=OwnedFeed(self.c,self.config_path,self.run_id)
            self.c.update(tq_run_id=self.run_id,tq_spool=str(self.tq_feed.spool))
        self.host_ticks=LatestQuotes() if priority(self.c) else queue.Queue(maxsize=10000);self.tick_subscriptions=set();self.tick_subscription_attempted=set()
        self.host_m1=queue.Queue(maxsize=1000);self.m1_subscriptions=set();self.m1_subscription_attempted=set()
        self.quote_feed_counts=Counter();self.accepted_quotes={};self.quote_feed_last={}
        self.counts=Counter();self.fill_notified=set();self.last_poll=0.;self.last_report=0.;self.max_gap=0.;self.last_loop=time.monotonic()
        self.notifications=None if readonly else FeishuOutbox(ROOT/self.c['notification_config'],self.evidence)
        self.cursor_path=Path(self.c['state_dir'])/'notification_cursor.json'
        orderfile=self.evidence/'order_events.jsonl'
        saved=json.loads(self.cursor_path.read_text(encoding='utf8')) if self.cursor_path.exists() else {}
        self.order_offset=saved.get('order_offset',orderfile.stat().st_size if orderfile.exists() else 0)
        self.start_notice=False;self.last_problem=None
        self.price_action_observer=None
        if not readonly:
            try:
                from futures_trend_quant.observation.price_action.worker import Observer
                self.price_action_observer=Observer(self.c['root'],self.run_id)
            except Exception as exc:
                self.event('PRICE_ACTION_SHADOW_UNAVAILABLE',category=type(exc).__name__,affects_execution=False)
        for cs in self.c['universe']:
            key=cs.replace('.','_');child=dict(self.c,csymbol=cs,quote_poll_enabled=False,
                state_dir=str(Path(self.c['state_dir'])/'products'/key),evidence_dir=str(self.evidence/'products'/key))
            Path(child['state_dir']).mkdir(parents=True,exist_ok=True);Path(child['evidence_dir']).mkdir(parents=True,exist_ok=True)
            product=dict(config=child,market=MarketWorker(child),core=SignalWorker(child),metadata=None,calendar=None,
                coverage={},ready=False,frozen=False,fault=None,counts=Counter(),last_positions=None,last_trend=None,received={})
            self.products[cs]=product
            cache=Path(child['evidence_dir'])/'market_metadata.local.json'
            if cache.exists():
                m=json.loads(cache.read_text(encoding='utf8'));cal=PaperCalendar(m['dates'],m['sessions'],cs);self.register(cs,m,cal)
        if self.calendar.contracts and not readonly:self.engine=PaperEngine(self.c,self.calendar,self.transport)
        self.started_with_pause=bool(self.engine and self.engine.s['pause_new'])
        self.quote_worker.start()
        if self.tq_feed is not None:self.tq_feed.start()
        for p in self.products.values():p['market'].thread.start();p['core'].thread.start()
        self.observation_started=self.started
        history=self.evidence/'observation_start.json'
        if history.exists():self.observation_started=stamp(json.loads(history.read_text())['started_at'])
        else:atomic(history,dict(started_at=self.started.isoformat(),pool=self.c['universe']))
        from .paper_service_health import ServiceHealth
        from .paper_symbol_health import service_policy
        self.service_health=ServiceHealth(self) if not readonly and self.c.get('clock_policy_version') else None
        manifest=dict(schema_version=2,run_id=self.run_id,started_at=self.started.isoformat(),readonly=readonly,service_policy_version=service_policy(self.c),
            symbol_market_policy=self.c.get('symbol_market_policy'),
            code_commit=code_revision(ROOT),
            universe=self.c['universe'],pool_policy_version=self.c.get('pool_policy_version'),strategy=self.c['strategy_name'],live_orders_enabled=False,config_sha256=self.c['config_sha256'],
            market='GM_FIVE_AND_TQ_FIVE_CLOSED_M1' if external else 'GM_BULK_CURRENT_PLUS_PER_PRODUCT_CLOSED_M1_POLL',streaming_subscriptions=0,
            external_market_policy=self.c.get('external_market_policy'),
            market_sources={cs:'TQ' if cs in external else 'GM' for cs in self.c['universe']},
            base_rules_unchanged=self.c['strategy_name']=='S0_SHORT_PAPER_V1',permission_policy=self.c.get('permission_policy'),
            review_mode='SINGLE_AGENT_SELF_CHECK',independent_score_run=False,trend_observation_version=self.c.get('trend_observation_version'),
            trend_observation_sha256_lf=hashlib.sha256((ROOT/'futures_trend_quant/live/trend_observation.py').read_bytes().replace(b'\r\n',b'\n')).hexdigest(),
            source_sha256_lf={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in (ROOT/'futures_trend_quant/live').glob('paper_*.py')})
        observer=getattr(self,'price_action_observer',None)
        from futures_trend_quant.observation.price_action.models import POLICY as pa_policy
        manifest['price_action_shadow']=None if observer is None else dict(policy=pa_policy,
            parameters_sha256=observer.health()['parameters_sha256'],mode='SHADOW',affects_execution=False)
        atomic(self.evidence/('run_manifest_'+self.run_id+'.json'),manifest);atomic(self.evidence/'run_manifest.json',manifest)
        self.clock_worker=None
        self.clock_worker_started_monotonic=None;self.clock_first_current_ready=False
        if self.c.get('clock_policy_version'):
            from .paper_clock_health import clock_process
            self.clock_worker=ctx.Process(target=clock_process,args=(str(self.evidence),self.c['config_sha256'],self.run_id,self.c.get('clock_sampler_policy'),self.c.get('clock_probe_retry_policy')),daemon=True,name='paper-clock-health')
            self.clock_worker.start()
            self.clock_worker_started_monotonic=time.monotonic()

    def accept_quote(self,symbol,q,received):
        from .paper_quote_feed import supersedes
        if symbol not in self.records:raise ValueError('QUOTE_CONTRACT_NOT_AUTHORIZED')
        from .paper_tq import external_product,SOURCE,final_source
        cs=product_of(symbol)
        if external_product(self.c,cs):
            final_source(self.c['root'],self.c,self.c['config_sha256'],symbol,q,pd.Timestamp.now(tz='UTC').to_pydatetime(),opening=False)
        elif q.get('source')==SOURCE:raise ValueError('EXTERNAL_QUOTE_WRONG_PRODUCT')
        if self.c.get('quote_policy_version'):
            previous=self.accepted_quotes.get(symbol)
            if not supersedes(self.accepted_quotes.get(symbol),q):
                from .paper_quote_audit import record
                record(self,symbol,q,received,previous,False)
                self.quote_feed_counts['OLDER_QUOTE_IGNORED']+=1;return False
            from .paper_quote_audit import record
            record(self,symbol,q,received,previous,True)
            self.accepted_quotes[symbol]=dict(q)
            self.quote_feed_counts[q['source']]+=1
            self.quote_feed_last[symbol]=dict(source=q['source'],event_at=q['created_at'],received_at=str(received))
            # Exactly one publisher per strategy: poll child only sends events.
            atomic(self.evidence/'latest_quotes'/(symbol+'.json'),dict(symbol=symbol,config_sha256=self.c['config_sha256'],host_run_id=getattr(self,'run_id',None),received_at=str(received),quote=q))
        if self.engine:self.engine.quote(symbol,q,received)
        applied=pd.Timestamp.now(tz='Asia/Shanghai')
        with (self.evidence/'quotes.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(dict(symbol=symbol,quote=q,local_received_at=str(received),
            coordinator_applied_at=str(applied),handoff_seconds=(applied-stamp(received)).total_seconds()))+'\n')
        return True

    def queue_host_tick(self,tick,received):
        from .paper_quote_feed import from_tick
        symbol,q=from_tick(tick,self.records)
        from .paper_tq import external_product
        if external_product(self.c,product_of(symbol)):raise ValueError('GM_TICK_NOT_ADMITTED_FOR_TQ_PRODUCT')
        self.quote_feed_counts['GM_HOST_TICK_RECEIVED']+=1
        try:self.host_ticks.put_nowait((symbol,q,str(received)))
        except queue.Full:
            from .paper_symbol_health import enabled as isolated
            if isolated(self.c):self.freeze_product(product_of(symbol),'TICK_QUEUE_OVERFLOW',received)
            elif self.engine:self.engine.pause_entries('TICK_QUEUE_OVERFLOW',received,'QUOTE_FEED')
            self.event('TICK_QUEUE_OVERFLOW')

    def accept_feed_quote(self,symbol,q,received):
        from .paper_symbol_health import enabled as isolated
        try:return self.accept_quote(symbol,q,received)
        except (ValueError,KeyError,TypeError) as exc:
            if not isolated(self.c):raise
            if symbol in self.records:
                FiveRunner.freeze_product(self,product_of(symbol),'QUOTE_INPUT_REJECTED',received,type(exc).__name__)
            else:self.event('UNREGISTERED_QUOTE_REJECTED',category=type(exc).__name__)
            return False

    def queue_host_bar(self,bars,received):
        from .paper_closed_m1 import from_callback,SOURCE
        for raw in bars:
            try:
                cs,bar=from_callback(raw,self.records,self.c,received)
                clocks=dict(source=SOURCE,requested_at='NOT_APPLICABLE_CALLBACK',returned_at=str(received),
                            enqueued_at=datetime.now(timezone.utc).isoformat(),host_run_id=self.run_id)
                self.host_m1.put_nowait((cs,bar,clocks))
            except (ValueError,KeyError,TypeError,queue.Full) as exc:
                from .paper_symbol_health import enabled as isolated
                if not isolated(self.c):raise
                try:
                    symbol=raw['symbol'];cs=product_of(symbol)
                    if symbol not in self.records:raise ValueError('UNREGISTERED_CALLBACK')
                    self.freeze_product(cs,'GM_CLOSED_M1_CALLBACK_INVALID',received,type(exc).__name__)
                except (ValueError,KeyError,TypeError):self.event('UNREGISTERED_GM_CALLBACK_REJECTED',category=type(exc).__name__)

    def freeze_product(self,cs,reason,now=None,detail=None):
        p=self.products[cs];changed=p.get('fault')!=reason
        p.update(frozen=True,fault=reason,ready=False)
        symbol=(p.get('metadata') or {}).get('symbol')
        if self.engine and symbol:self.engine.invalidate_candidates(symbol,reason,now or pd.Timestamp.now(tz='Asia/Shanghai'))
        if changed:
            self.event('PRODUCT_MARKET_QUARANTINED',csymbol=cs,reason=reason,detail=detail)
            self.notice('product-market:'+self.run_id+':'+cs+':'+reason,'单品种行情不可用',
                [PRODUCTS[cs]['name']+' '+str(symbol),reason,'仅该品种停止信号展示和新开；账户与既有持仓退出保护保留。'])

    def apply_m1(self,cs,bars,clocks,now):
        from .paper_closed_m1 import enabled,ClosedM1Book
        from .paper_tq import external_product
        p=self.products[cs]
        from .paper_gm_handoff import enabled as priority
        if priority(self.c) and not external_product(self.c,cs) and (p['calendar'] is None or not p.get('ready')) and clocks.get('source')=='GM_HOST_ON_BAR_CLOSED_M1':
            from dataclasses import asdict
            pending=p.setdefault('gm_warmup_pending',{})
            for bar in bars:
                old=pending.get(bar.identity)
                if old and old[0]!=bar:raise ValueError('GM_WARMUP_CALLBACK_REVISION')
                if old:continue
                if len(pending)>=1000:raise ValueError('GM_WARMUP_CALLBACK_OVERFLOW')
                from .paper_tq import append
                raw=asdict(bar);raw.update(bob=bar.bob.isoformat(),eob=bar.eob.isoformat())
                append(Path(p['config']['evidence_dir'])/'gm_warmup_pending.jsonl',dict(bar=raw,clocks=clocks,
                    captured_at=datetime.now(timezone.utc).isoformat(),host_run_id=self.run_id,consumed=False))
                pending[bar.identity]=(bar,dict(clocks))
            self.event('GM_CLOSED_M1_PENDING_CORE',csymbol=cs,core_status=p['core'].progress.get('status'),pending=len(pending))
            return
        if p['calendar'] is None:return
        incoming=[(bar,dict(clocks)) for bar in bars]
        if enabled(self.c) and not external_product(self.c,cs):
            book=p.get('gm_closed_m1_book')
            baseline=p['core'].progress.get('last_closed_m1')
            if book is None and p.get('ready') and baseline:
                book=ClosedM1Book(stamp(baseline).to_pydatetime(),p['calendar']);p['gm_closed_m1_book']=book
            if book is not None:incoming=book.add(bars,clocks)
            elif clocks.get('source')=='GM_HOST_ON_BAR_CLOSED_M1':
                # The existing history warmup owns initialization. Never let a
                # live callback jump over its unproven frozen prefix.
                self.event('GM_HOST_M1_WAIT_CORE_WARMUP',csymbol=cs);return
        groups=[]
        for bar,proof in incoming:
            if groups and groups[-1][1]==proof:groups[-1][0].append(bar)
            else:groups.append(([bar],proof))
        for members,proof in groups:
            applied_at=pd.Timestamp.now(tz='Asia/Shanghai').isoformat()
            for bar in members:
                if self.engine:self.engine.m1(bar,stamp(applied_at))
                day=p['calendar'].day(bar.bob);p['received'].setdefault(day,set()).add(str(bar.eob))
                with (Path(p['config']['evidence_dir'])/'m1_observed.jsonl').open('a',encoding='utf8') as f:
                    f.write(json.dumps(dict(symbol=bar.symbol,bob=str(bar.bob),eob=str(bar.eob),open=bar.open,high=bar.high,low=bar.low,close=bar.close,
                        volume=bar.volume,amount=bar.amount,position=bar.position,local_observed_at=applied_at,
                        coordinator_cycle_at=str(now),**proof,applied_at=applied_at,purpose='PAPER_OBSERVATION_SEEN'))+'\n')
            p['core'].inbox.put(('NEW',members,dict(proof,applied_at=applied_at)))
            self.observe('BARS',csymbol=cs,bars=members,known_at=applied_at)

    def supervise_clock(self,now):
        if not self.c.get('clock_policy_version'):return
        from .paper_clock_health import health,startup_wait,STARTUP_POLICY
        h=health(self.c);self.runtime_clock_health=h
        if self.c.get('clock_startup_policy')==STARTUP_POLICY:
            alive=bool(self.clock_worker and self.clock_worker.is_alive())
            if not alive:h.update(status='BLOCKED',reason='SYSTEM_CLOCK_UNTRUSTED',detail='CLOCK_WORKER_EXITED')
            if not h['reason']:self.clock_first_current_ready=True
            waiting=startup_wait(self.c,h,getattr(self,'clock_worker_started_monotonic',None),
                alive,getattr(self,'clock_first_current_ready',False))
            h['startup_wait']=waiting
            h['worker_started_monotonic']=getattr(self,'clock_worker_started_monotonic',None)
            h['first_current_ready']=getattr(self,'clock_first_current_ready',False)
            fault=bool(h['reason'] and not waiting)
        else:fault=bool(h['reason'] and (stamp(now)-self.started).total_seconds()>60)
        if fault:
            if self.engine:self.engine.pause_entries(h['reason'],now,'CLOCK_HEALTH')
            if getattr(self,'clock_fault_reported',None)!=h['reason']:
                self.event('CLOCK_HEALTH_FAULT',reason=h['reason'],detail=h.get('detail','UNKNOWN'),diagnostic=h.get('diagnostic'))
                atomic(self.evidence/('clock_failure_'+self.run_id+'_'+now.strftime('%H%M%S%f')+'.local.json'),
                       dict(at=str(now),run_id=self.run_id,config_sha256=self.c['config_sha256'],health=h,
                            policy='FAILED_READ_IS_NOT_A_TIME_SAMPLE;NO_STALE_PROOF_FALLBACK'))
                self.notice('clock:'+self.run_id+':'+h['reason'],'时钟异常·停止新开',['报价3秒时效依赖可靠时钟；不修改行情时间、不自动校时。',h.get('detail','时钟来源或偏差未通过'),'已知敞口继续由原执行链监督，恢复需要受控验收。'])
                self.clock_fault_reported=h['reason']
        if not h['reason']:
            if getattr(self,'clock_fault_reported',None):
                self.event('CLOCK_HEALTH_RECOVERED',pause_retained=bool(self.engine and self.engine.s['pause_new']))
            self.clock_fault_reported=None
        self.clock_problem=h['reason']

    def register(self,cs,meta,calendar):
        record=contract_record(meta,calendar)
        if product_of(meta['symbol'])!=cs:raise ValueError('metadata product mismatch')
        self.products[cs].update(metadata=meta,calendar=calendar)
        self.observe('METADATA',csymbol=cs,metadata=meta)
        self.products[cs]['core'].inbox.put(('CALENDAR',meta))
        self.calendar.register(meta['symbol'],calendar);self.records[meta['symbol']]=record
        atomic(Path(self.c['state_dir'])/'market_registry.local.json',dict(config_sha256=self.c['config_sha256'],contracts=self.records))
        self.quote_in.put(dict(self.records))

    def notice(self,key,title,lines,tag='STRATEGY',fields=None,reasons=None,execution=None,local_only=False,severity=None,priority=None):
        if self.notifications:return self.notifications.enqueue(key,message(title,lines,tag,fields,reasons,execution,severity),deliver=not local_only,priority=priority)

    def drain_gm_pending(self,cs,now):
        from .paper_cycle_work import take
        p=self.products[cs];pending=p.get('gm_warmup_pending',{});drained=0
        if not p.get('ready'):return
        for key,(bar,clocks) in sorted(list(pending.items()),key=lambda item:item[1][0].eob):
            if not take(self,'GM_WARMUP_DRAIN',cs,per_lane=8):break
            FiveRunner.apply_m1(self,cs,[bar],clocks,now)
            pending.pop(key);drained+=1
        if drained:self.event('GM_CLOSED_M1_PENDING_DRAINED',csymbol=cs,rows=drained,remaining=len(pending),original_clocks_retained=True)

    def market_events(self,now,csymbol=None):
        from .paper_cycle_work import products,take
        for cs,p in products(self,'market'):
            if csymbol is not None and cs!=csymbol:continue
            if p.get('gm_warmup_pending'):FiveRunner.drain_gm_pending(self,cs,now)
            while take(self,'MARKET_DATA',cs):
                try:e=p['market'].events.get_nowait()
                except queue.Empty:break
                kind=e[0]
                if kind=='METADATA':
                    try:self.register(cs,e[1],e[2])
                    except Exception as exc:
                        p.update(frozen=True,fault='METADATA_INVALID');self.event('METADATA_INVALID',csymbol=cs,category=type(exc).__name__);continue
                    if self.engine is None and not self.readonly:
                        self.engine=PaperEngine(self.c,self.calendar,self.transport)
                        self.started_with_pause=bool(self.engine.s['pause_new'])
                elif kind=='INITIAL_CACHE':
                    p['coverage']=e[2];p['core'].inbox.put(('INITIAL_CACHE',e[1]))
                    self.observe('CACHE',csymbol=cs,known_at=pd.Timestamp.now(tz='Asia/Shanghai').isoformat(),**e[1])
                    self.event('PRODUCT_WARMUP',csymbol=cs,coverage=e[2])
                elif kind=='NEW_BARS':
                    clocks=e[2] if len(e)>2 else dict(requested_at='UNKNOWN',returned_at='UNKNOWN',enqueued_at='UNKNOWN')
                    from .paper_symbol_health import enabled as isolated
                    try:FiveRunner.apply_m1(self,cs,e[1],clocks,now)
                    except (ValueError,KeyError,TypeError,OSError) as exc:
                        if not isolated(self.c):raise
                        FiveRunner.freeze_product(self,cs,'CLOSED_DATA_REJECTED',now,str(exc)[:160])
                elif kind=='SOURCE_STATUS':
                    p['source_ready']=e[1]['ready'];p['source_status']=e[1]
                    if not e[1]['ready'] and self.engine and p.get('metadata'):self.engine.invalidate_candidates(p['metadata']['symbol'],'SYMBOL_SOURCE_NOT_READY',now)
                elif kind=='QUOTE':
                    try:self.accept_quote(e[1],e[2],e[3])
                    except ValueError as exc:
                        p.update(frozen=True,fault='TQ_QUOTE_REJECTED')
                        self.event('TQ_QUOTE_REJECTED',csymbol=cs,category=type(exc).__name__)
                elif kind in ('DATA_FAILED','DATA_GAP','ROLLOVER_FREEZE'):
                    p.update(frozen=True,fault=kind);self.event(kind,csymbol=cs,detail=e[1])
                    self.notice('data:'+self.run_id+':'+cs+':'+kind,'单品种停止新开',[PRODUCTS[cs]['name']+' '+cs,kind,'已知持仓仍由账户执行器监督退出；其余品种独立检查。'])
        if csymbol is not None and csymbol!='__FEEDS_ONLY__':return
        from .paper_gm_handoff import enabled as priority
        if priority(self.c):
            from .paper_tq import external_product
            for symbol in self.records:
                if external_product(self.c,product_of(symbol)):continue
                path=self.evidence/'quote_poll_latest'/self.run_id/(symbol+'.json')
                try:row=read_json(path)
                except FileNotFoundError:continue
                except (OSError,ValueError) as exc:
                    self.event('QUOTE_POLL_LATEST_REJECTED',symbol=symbol,category=type(exc).__name__);continue
                if row.get('host_run_id')!=self.run_id or row.get('config_sha256')!=self.c['config_sha256'] or row.get('symbol')!=symbol:
                    self.event('QUOTE_POLL_LATEST_REJECTED',symbol=symbol,category='IDENTITY');continue
                if not isinstance(row.get('quote'),dict) or not row.get('received_at') or not row['quote'].get('created_at'):
                    self.event('QUOTE_POLL_LATEST_REJECTED',symbol=symbol,category='FIELDS');continue
                seen=getattr(self,'_poll_latest_seen',{});identity=(row['received_at'],row['quote']['created_at'])
                if seen.get(symbol)==identity:continue
                FiveRunner.accept_feed_quote(self,symbol,row['quote'],row['received_at'])
                seen[symbol]=identity;self._poll_latest_seen=seen
        while getattr(self,'host_m1',None) is not None and take(self,'GM_CLOSED_M1','host',per_lane=16):
            try:cs,bar,clocks=self.host_m1.get_nowait()
            except queue.Empty:break
            try:FiveRunner.apply_m1(self,cs,[bar],clocks,now)
            except (ValueError,KeyError,TypeError) as exc:
                self.products[cs].update(frozen=True,fault='GM_CLOSED_M1_REJECTED')
                self.event('GM_CLOSED_M1_REJECTED',csymbol=cs,detail=str(exc)[:180])
                from .paper_symbol_health import enabled as isolated
                if isolated(self.c):FiveRunner.freeze_product(self,cs,'GM_CLOSED_M1_REJECTED',now,str(exc)[:160])
                elif self.engine:self.engine.pause_entries('GM_CLOSED_M1_REJECTED',source='GM_HOST')
        while getattr(self,'host_ticks',None) is not None and not self.host_ticks.empty() and take(self,'HOST_TICKS','host',per_lane=64):
            try:symbol,q,received=self.host_ticks.get_nowait()
            except queue.Empty:break
            FiveRunner.accept_feed_quote(self,symbol,q,received)
        while take(self,'QUOTE_POLL','poll',per_lane=32):
            try:e=self.quote_events.get_nowait()
            except queue.Empty:break
            if e[0]=='QUOTE':
                _,symbol,q,received=e
                FiveRunner.accept_feed_quote(self,symbol,q,received)
            else:self.event('QUOTE_SOURCE_ERROR',category=e[1],consecutive=e[2])

    def core_events(self,now,csymbol=None):
        from .paper_cycle_work import products,take
        for cs,p in products(self,'core'):
            if csymbol is not None and cs!=csymbol:continue
            while take(self,'CORE_DATA',cs,per_lane=4):
                try:e=p['core'].outbox.get_nowait()
                except queue.Empty:break
                kind=e[0]
                if kind in ('CORE_PROGRESS','CORE_READY'):
                    p['core'].progress=e[1]
                    if kind=='CORE_READY':
                        p['ready']=True
                        FiveRunner.drain_gm_pending(self,cs,pd.Timestamp.now(tz='Asia/Shanghai'))
                elif kind in ('WARMUP_FAILED','CORE_FAILED'):
                    p.update(ready=False,frozen=True,fault=kind);self.event(kind,csymbol=cs,detail=e[1])
                    self.notice('core:'+self.run_id+':'+cs+':'+kind,'策略数据不可用',[PRODUCTS[cs]['name']+' '+cs,kind,'该品种暂停新开，不填零产生信号。'])
                elif kind=='CORE_RESULT':
                    result,bar=e[1],e[2];now=pd.Timestamp.now(tz='Asia/Shanghai');entry=result.entry;decision=None
                    from .paper_symbol_health import display_status,enabled as isolated
                    cfg=getattr(self,'c',{})
                    signal_display=display_status(cfg.get('root',ROOT),cs,now,bar_eob=bar.eob) if isolated(cfg) else dict(ready=True,reason=None,policy='LEGACY_DISPLAY')
                    signal=None
                    execution_diagnostic=dict(product_ready=bool(p.get('ready')),coverage_qualified=bool((p.get('coverage') or {}).get('qualified')),
                        frozen=bool(p.get('frozen')),fault=p.get('fault'),engine_present=self.engine is not None,
                        anchor_present=getattr(result,'anchor',None) is not None,signal_display=signal_display)
                    p['counts']['M15_DECISIONS']+=1;self.counts['M15_DECISIONS']+=1
                    explanation=reason_context(result,e[4] if len(e)>4 else None)
                    if self.engine and result.trend.confirmed:self.engine.force_exit(bar.symbol,'TREND_EXIT',now,direction=result.trend.confirmed_state,context=explanation)
                    permission=getattr(result,'permission',None)
                    opening_permission=getattr(result,'entry_permission',None) if getattr(self,'c',{}).get('entry_policy_version')=='PAPER_ENTRY_V5' else permission
                    if self.engine and permission is not None:
                        if not opening_permission or not opening_permission.new_entry_allowed:
                            self.engine.invalidate_candidates(bar.symbol,'SHARED_PERMISSION_CHANGED',now)
                        if permission.technical_exit_direction:
                            # force_exit(direction=...) preserves that direction; pass
                            # the opposite to reduce the specifically weakened side.
                            preserve='bear' if permission.technical_exit_direction=='bull' else 'bull'
                            self.engine.force_exit(bar.symbol,'TECHNICAL_WEAKNESS_EXIT_V1',now,direction=preserve,context=explanation)
                    if result.trend_changed and signal_display['ready']:
                        self.notice('trend:'+cs+':'+bar.eob.isoformat(),'趋势方向变更',reason_lines(explanation),
                            fields=[('标的',PRODUCTS[cs]['name']+' '+bar.symbol),('方向变化',str(result.prior_trend)+' → '+str(result.trend.confirmed_state)),
                                ('M15收盘时间',bar.eob.isoformat()),('入场判定',str(None if entry is None else entry.reason))],
                            execution=['方向授权变化不代表已有成交；入场、风控和委托回报分别核验。'],local_only=self.c.get('notification_policy_version')==CARD_POLICY)
                    if entry is not None and entry.accepted:
                        p['counts']['TECHNICAL_SIGNALS']+=1
                        if self.engine and p['ready'] and p['coverage'].get('qualified') and not p['frozen'] and result.anchor is not None:
                            spec=p['metadata']['spec'];atr=result.analysis.indicators.atr14[-1]
                            signal=dict(symbol=bar.symbol,csymbol=cs,direction=entry.direction,
                                evidence_id=cs+'|'+entry.evidence_id,core_evidence_id=entry.evidence_id,signal_at=bar.eob.isoformat(),
                                anchor=result.anchor.anchor_price,atr=float(atr),multiplier=float(spec['multiplier']),margin_ratio=float(spec['margin_ratio']),
                                tick=float(spec['price_tick']),
                                rules=self.c['base_config'].raw['rules'],path=entry.path,m15_index=result.m15_index,
                                anchor_bar_id=result.anchor.anchor_bar_id,
                                anchor_confirmed_at=(result.anchor.confirmed_at_bar_id or '').rsplit('|',1)[-1],
                                anchor_episode_id=result.anchor.episode_id,trend_episode_id=result.trend.state.since_bar_id,
                                anchor_validated=bool(result.anchor.confirmed_at_bar_id and result.anchor.episode_id))
                            signal.update(dict(getattr(result,'evidence_clock',None) or {}))
                            signal['decision_at']=str(e[3])
                            signal.update(entry_permission=bool(opening_permission and opening_permission.new_entry_allowed),
                                permission_policy=None if opening_permission is None else opening_permission.policy,
                                permission_eob=bar.eob.isoformat(),permission_direction=None if opening_permission is None else opening_permission.background,
                                entry_policy_proof=getattr(result,'entry_policy_proof',None))
                            if signal.get('first_eligible_at'):signal['signal_at']=signal['first_eligible_at']
                            signal['trade_reason']=explanation
                            decision=self.engine.offer(signal,now)
                        else:decision=(False,'PRODUCT_NOT_READY_OR_READONLY')
                        p['counts']['ORDER_ALLOWED' if decision[0] else 'BLOCKED_'+decision[1]]+=1
                        stale_candidate=bool(getattr(result,'evidence_clock',None) and stamp(now)>stamp(result.evidence_clock['expires_at']))
                        if not stale_candidate and signal_display['ready'] and self.c.get('notification_policy_version')!=CARD_POLICY:self.notice('signal:'+cs+':'+entry.evidence_id,'入场候选',[PRODUCTS[cs]['name']+' '+bar.symbol,
                            ('做多' if entry.direction=='bull' else '做空')+'；路径 '+str(entry.path),'信号收盘 '+bar.eob.isoformat(),
                            '计算完成 '+str(e[3]),'处理：'+('已生成委托意图，尚未确认成交' if decision[0] else '未报单：'+decision[1]),
                            '每笔最多1手；实际成交另发通知；D+1和休市前清仓。'],
                            fields=[('标的',PRODUCTS[cs]['name']+' '+bar.symbol),('方向',DIRECTIONS.get(entry.direction,entry.direction)),
                                ('动作／状态','候选；委托意图已生成' if decision[0] else '候选；未报单 '+decision[1]),('触发路径',str(entry.path)),
                                ('信号收盘时间',bar.eob.isoformat()),('计划手数','最多1手，同品种不加仓')],
                            reasons=reason_lines(explanation),
                            execution=['处理结果 '+str(decision),'计算完成 '+str(e[3]),'实际成交、止损目标与截止时点以成交卡片及柜台持仓为准。'])
                    row=dict(csymbol=cs,bar_eob=bar.eob.isoformat(),signal_ready_at=e[3],order_evaluated_at=str(now),facts=result.facts_row,signal_display=signal_display,
                        execution_diagnostic=execution_diagnostic,execution_evidence_id=None if signal is None else signal['evidence_id'],
                        decision_stage='CANDIDATE_QUEUE_INTERMEDIATE' if decision and decision[1]=='CANDIDATE_WAITING' else 'LOCAL_DECISION;SEE_ORDER_EVENTS_AND_NATIVE_TRACE',
                        trend_observation=explanation.get('trend_observation'),
                        entry=None if entry is None else entry.__dict__,paper_decision=decision)
                    if entry is not None and entry.accepted and self.c.get('notification_policy_version')==CARD_POLICY:
                        frozen_signal=signal or dict(symbol=bar.symbol,csymbol=cs,direction=entry.direction,path=entry.path,
                            signal_at=bar.eob.isoformat(),anchor=None,atr=None,trade_reason=explanation,
                            **dict(getattr(result,'evidence_clock',None) or {}))
                        plan_at=pd.Timestamp.now(tz='Asia/Shanghai')
                        q,why=self.engine.valid_quote(bar.symbol,plan_at) if self.engine else (None,'EXECUTION_NOT_READY')
                        plan=freeze_plan(frozen_signal,bar.close,q,why,plan_at,self.c,
                            None if not self.engine or not self.engine.snapshot else self.engine.snapshot['cash']['nav'])
                        raw_quote=None if not self.engine else self.engine.quotes.get(bar.symbol)
                        proof=dict(checked_at=str(plan_at),reason=why,source=None if not raw_quote else raw_quote.get('source'),
                            event_at=None if not raw_quote else raw_quote.get('created_at'),received_at=None if not raw_quote else raw_quote.get('received_at'))
                        for field,age_name in [('created_at','event_age_seconds'),('received_at','receive_age_seconds')]:
                            try:proof[age_name]=None if not raw_quote else (plan_at-stamp(raw_quote[field])).total_seconds()
                            except (KeyError,TypeError,ValueError):proof[age_name]=None
                        plan['quote_proof']=proof
                        notice_decision=decision
                        if signal is None:plan.update(invalid='EXECUTION_SNAPSHOT_UNAVAILABLE',invalid_text=reason_text(decision[1]))
                        if self.engine:
                            known_block=self.engine.s['halt_reason'] or ('STOP_NEW_OPEN' if self.engine.s['pause_new'] else self.engine.cal(bar.symbol).entry_block(now))
                            if known_block:notice_decision=(False,known_block)
                        row.update(notice_schema='CANDIDATE_CARD_V4',candidate_plan=plan)
                        if self.notifications and signal_display['ready'] and stamp(now)<=stamp(plan['expires_at']):
                            key='candidate-v4:'+cs+'|'+entry.evidence_id+'|'+plan['first_eligible_at']
                            self.notifications.enqueue(key,candidate_card(plan,notice_decision,now,PRODUCTS[cs]['name']),expires_at=plan['expires_at'],priority=3)
                    with (self.evidence/'signals.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
                    if entry is not None and entry.accepted:
                        plan=row.get('candidate_plan') or {}
                        self.observe('CANDIDATE',csymbol=cs,known_at=str(now),candidate=dict(
                            candidate_evidence_id=cs+'|'+entry.evidence_id,direction=entry.direction,path=entry.path,
                            bar_eob=bar.eob.isoformat(),
                            entry=plan.get('entry'),stop=plan.get('stop'),multiplier=(signal or {}).get('multiplier'),
                            anchor=plan.get('anchor'),anchor_bar_id=plan.get('anchor_bar_id'),target=plan.get('target'),
                            quote_known=plan.get('quote_valid',False),costs=None,
                            entry_policy_proof=(signal or {}).get('entry_policy_proof'),
                            original_expires_at=plan.get('expires_at'),original_decision=decision))

    def observe(self,kind,**payload):
        # SHADOW errors never enter engine / account / risk paths.
        observer=getattr(self,'price_action_observer',None)
        if observer is not None:
            try:observer.offer(kind,**payload)
            except Exception:
                observer.dropped+=1

    def pump_notifications(self):
        if not self.notifications or not self.engine:return
        path=self.evidence/'order_events.jsonl'
        if not path.exists():return
        with path.open('rb') as f:
            f.seek(self.order_offset)
            for raw in f:
                e=json.loads(raw);kind=e['kind'];tag=e.get('tag','STRATEGY')
                if kind=='ORDER_INTENT' and e['intent']['effect']==1 and self.c.get('notification_policy_version')==CARD_POLICY:
                    i=e['intent'];plan=intent_plan(i,self.c)
                    self.notifications.enqueue('intent-plan-v4:'+i['id'],candidate_card(plan,(True,i['id']),e['recorded_at'],PRODUCTS[i['csymbol']]['name'],stage='ORDER_INTENT',tag=i['tag']),priority=2,intent_id=i['id'])
                elif kind=='EXECUTION_REPORT':
                    fill=e['execution'];i=next((x for x in self.engine.s['intents'].values() if x.get('order_id')==fill['cl_ord_id']),None)
                    if i:
                        if hasattr(self.notifications,'supersede_intent'):self.notifications.supersede_intent(i['id'],'FILL_OBSERVED')
                        meta=self.engine.s['meta'].get(self.engine.key(i['symbol'],i['direction']),{})
                        protection=fill_protection(i,meta,fill)
                        opening=i['effect']==1;reason=i.get('trade_reason',{}) if opening else i.get('exit_evidence',{})
                        why=reason_lines(reason) if opening and reason else [EXITS.get(i.get('reason'),str(i.get('reason','原因待核')))]
                        if not opening and reason:why+=reason_lines(reason)
                        self.notice('fill:'+fill['exec_id'],'入场已成交' if opening else '离场已成交',[
                            PRODUCTS[i['csymbol']]['name']+' '+i['symbol']+' · '+('多' if i['direction']=='bull' else '空'),
                            '成交 '+str(fill['volume'])+' 手，价格 '+str(fill['price']),'平台模拟手续费 '+str(fill.get('commission','UNKNOWN')),
                            '保护止损 '+number(protection.get('stop'))+'；2R目标 '+number(protection.get('target')),
                            '最迟启动清仓 '+str(protection.get('deadline','未取得')),
                            '原因 '+str(i.get('reason',i.get('path','UNKNOWN'))),
                            '成交回报不替代柜台余量核对；净经济性尚未验证。'],i['tag'],
                            fields=[('关联意图',intent_reference(i['id'])),('标的',PRODUCTS[i['csymbol']]['name']+' '+i['symbol']),('持仓方向',DIRECTIONS[i['direction']]),
                                ('明确动作',('买入开多' if i['side']==1 else '卖出开空') if opening else ('卖出平多' if i['side']==2 else '买入平空')),('模拟结果','实际成交回报，尚需余量对账'),
                                ('成交价格',number(fill['price'])),('成交手数',str(fill['volume'])),
                                ('保护止损',number(protection.get('stop'))),('2R目标',number(protection.get('target'))),
                                ('平台本次扣费',number(fill.get('commission'))),('清仓截止',str(protection.get('deadline','未取得')))]+closing_gross(i,fill),
                            reasons=why,execution=['信号时间 '+time_text(i.get('signal_at',meta.get('opened_at','UNKNOWN'))),
                                '平台成交时点 '+time_text(fill.get('created_at','UNKNOWN')),'本机收到回报 '+time_text(e['recorded_at']),
                                '相对发令对手价偏差 '+str(i.get('fill_vs_quoted_opponent_ticks','UNKNOWN'))+' tick；不代表真实冲击成本。','价差毛盈亏未扣开平两侧全部费用，不等同真实净收益。'])
                elif kind=='ORDER_STATUS' and e['status']==1:
                    i=next((x for x in self.engine.s['intents'].values() if x.get('order_id')==e['order_id']),None)
                    if i and hasattr(self.notifications,'supersede_intent'):self.notifications.supersede_intent(i['id'],'BROKER_ACKNOWLEDGED')
                    if i:self.notice('ack:'+e['order_id'],'委托已受理',['柜台已受理委托，尚不能视作成交。'],i['tag'],
                        fields=[('关联意图',intent_reference(i['id'])),('标的',PRODUCTS[i['csymbol']]['name']+' '+i['symbol']),('动作','开仓' if i['effect']==1 else '平仓'),
                            ('方向',DIRECTIONS[i['direction']]),('委托手数','1'),('限价',number(i['price'])),('状态','已受理／等待成交')]+
                            ([('委托止损计划',number(i.get('stop'))),('委托2R目标',number(i.get('target')))] if i['effect']==1 else []),
                        reasons=reason_lines(i.get('trade_reason',{})) if i['effect']==1 else [EXITS.get(i.get('reason'),str(i.get('reason')))],
                        execution=['报价事件 '+time_text(i.get('decision_quote',{}).get('created_at','UNKNOWN')),'本机受理观测 '+time_text(i.get('ack_received_at','UNKNOWN'))])
                elif kind=='EXIT_DUE':
                    self.notice('due:'+e['symbol']+':'+e['recorded_at'],'退出待完成',[e['symbol'],'原因 '+e['reason'],'应减余量 '+str(e['quantity'])+' 手','持续核对成交与柜台持仓；未平风险仍保留。'])
                elif kind=='CANDIDATE_FINISHED':
                    if e['status']=='SUBMITTED' and self.c.get('notification_policy_version')==CARD_POLICY:continue
                    self.notice('candidate-finished:'+e['evidence_id'],'候选终止·未下单' if self.c.get('notification_policy_version')==CARD_POLICY else '候选执行结果',[
                        '证据 '+e['evidence_id'],'结果 '+e['status'],'原因 '+reason_text(e['reason']),
                        '等待不会延长原30秒有效期；SUBMITTED只代表委托意图，成交另行确认。'],
                        fields=[('标的 / 路径',str(e.get('symbol','UNKNOWN'))+' / '+str(e.get('path','UNKNOWN'))),
                            ('最终执行状态',str(e['status'])+'；'+reason_text(e['reason'])),
                            ('首次合格 / 原截止',time_text(e.get('first_eligible_at','UNKNOWN'))+' / '+time_text(e.get('expires_at','UNKNOWN')))],
                        execution=['终态来自实际执行队列，不以信号流水初态CANDIDATE_WAITING代替。','没有委托意图和柜台受理回报，就不能视为已下单。'])
                elif kind=='EXIT_FLAT_CONFIRMED':
                    self.notice('flat:'+e['symbol']+':'+e['recorded_at'],'柜台确认已平',[e['symbol'],'该合约方向余量为0，且无可再开仓活动委托。'])
                elif kind=='ENTRY_LOCAL_REJECTED':
                    if hasattr(self.notifications,'supersede_intent'):self.notifications.supersede_intent(e['rpc_id'],'LOCAL_REJECTED_BEFORE_NATIVE')
                    self.notice('local-reject:'+e['rpc_id'],'入场终止·本地拒绝，未下单',[
                        '关联本地意图 '+intent_reference(e['rpc_id']),'标的 '+e['symbol']+'；路径 '+str(e.get('path')),
                        '最终本地校验拒绝；未调用柜台下单，柜台无此挂单，成交0手。',
                        '具体原因 '+reason_text(e['reason']),
                        '原截止 '+time_text(e.get('expires_at','UNKNOWN'))+'；本信号不补发，不切换路径补单。'],
                        fields=[('关联意图',intent_reference(e['rpc_id'])),('标的 / 路径',e['symbol']+' / '+str(e.get('path'))),
                            ('最终状态','LOCAL_REJECTED_BEFORE_NATIVE；未下单'),('原因',reason_text(e['reason'])),
                            ('原计划价 / 止损 / 目标',number(e['price'])+' / '+number(e.get('stop'))+' / '+number(e.get('target')))],severity='warning')
                elif kind=='ENTRY_SUBMIT_UNKNOWN':
                    if hasattr(self.notifications,'supersede_intent'):self.notifications.supersede_intent(e['rpc_id'],'SUBMIT_OUTCOME_UNKNOWN')
                    self.notice('submit-unknown:'+e['rpc_id'],'提交结果未知·禁止重发',[
                        '关联意图 '+intent_reference(e['rpc_id'])+'；标的 '+e['symbol'],
                        '原因 '+str(e.get('reason') or 'UNKNOWN'),
                        '可能已到达柜台，尚未确认受理或成交；保持对账与持仓退出监督，不将未知敞口按零处理。',
                        '本信号不重发，不切换路径补单，不刷新截止。'],severity='critical')
                elif kind in ('RPC_FAILED','RECON_FAILED','UNOWNED_RESIDUAL_RISK_REQUIRES_RECON'):
                    if kind=='RPC_FAILED' and e.get('op')=='submit':continue
                    reason=str(e.get('reason') or e.get('error') or kind)
                    submitted=e.get('native_submit_invoked','UNKNOWN')
                    stage=('最终本地校验拒绝，未调用柜台下单' if submitted is False and e.get('op')=='submit' else
                           '提交结果待对账，禁止自动重发' if e.get('op')=='submit' else '账户查询或对账异常')
                    self.notice('execution-risk:'+kind+':'+reason+':'+str(int(time.time()//600)),'执行或对账异常',[
                        stage,kind,reason,'新开仓保持禁止或待核；已知持仓只做可确认减仓，未知敞口不按零处理。'])
                elif kind=='ORDER_STATUS' and e['status'] in (5,8,12):
                    self.notice('terminal:'+e['order_id']+':'+str(e['status']),'撤单或拒单回报',[
                        '状态码 '+str(e['status'])+'；累计成交 '+str(e['filled'])+' 手',
                        '拒绝原因 '+str(e.get('reject_detail') or e.get('reject_reason') or '无／不适用'),
                        '已成交部分继续管理；状态以柜台对账为准。'],tag)
            self.order_offset=f.tell()
        atomic(self.cursor_path,dict(order_offset=self.order_offset))

    def report(self,now):
        broker=None if self.engine is None else self.engine.summary();rows={};slots=[]
        for cs,p in self.products.items():
            m=p['metadata'];cal=p['calendar']
            ready=p['ready'] and p['coverage'].get('qualified') and not p['frozen']
            rows[cs]=dict(name=PRODUCTS[cs]['name'],symbol=None if m is None else m['symbol'],ready=bool(ready),frozen=p['frozen'],fault=p['fault'],
                source_ready=p.get('source_ready'),source_status=p.get('source_status'),
                warmup=p['coverage'],core=p['core'].progress,counts=dict(p['counts']),received_m1_minutes={d:len(v) for d,v in p['received'].items()},
                market_alive=p['market'].thread.is_alive(),core_alive=p['core'].thread.is_alive())
            if cal:
                from .paper_watermark import closure_status
                from .paper_tq import external_product
                rows[cs]['data_closure']=closure_status(cal,p['core'].progress,now,'TQ' if external_product(self.c,cs) else 'GM')
            if cal:slots.extend(elapsed_review_slots(cal,self.observation_started,now))
        slots=sorted(set(slots));ready_count=sum(x['ready'] for x in rows.values())
        if self.engine and len(slots)>=20:self.engine.s['pause_new']=True;self.engine.save()
        self.supervise_trend_notifier(now,ready_count)
        self.supervise_notification_sender(now)
        broker=None if self.engine is None else self.engine.summary()
        phase='READONLY_PREFLIGHT_PASS' if self.readonly and self.verified and ready_count==len(self.c['universe']) else 'READONLY_PREFLIGHT_PENDING' if self.readonly else (
            'PENDING_ACCOUNT_RECON' if not self.engine or not self.engine.ready else 'STOP_NEW_OPEN_MANAGING_POSITIONS' if self.engine.s['pause_new'] or self.engine.s['halt_reason'] or broker.get('new_entry_cooldown_reason') else
            'NATURAL_SIGNAL_OBSERVATION' if ready_count==len(self.c['universe']) else 'PARTIAL_PRODUCT_READINESS')
        status=dict(at=str(now),phase=phase,universe=self.c['universe'],pool_policy_version=self.c.get('pool_policy_version'),products=rows,broker=broker,ready_products=ready_count,config_sha256=self.c['config_sha256'],
            live_orders_enabled=False,quote_worker_alive=self.quote_worker.is_alive(),streaming_subscriptions=len(getattr(self,'tick_subscriptions',set())),maximum_main_loop_gap_seconds=self.max_gap,
            elapsed_complete_trading_day_slots=slots,five_day_review_due=len(slots)>=5,twenty_day_review_due=len(slots)>=20,
            notifications=None if not self.notifications else self.notifications.health(),independent_score_run=False,review_mode='SINGLE_AGENT_SELF_CHECK',
            trend_observation_version=self.c.get('trend_observation_version'))
        status['platform_connections']=dict(getattr(self,'platform_connections',{}))
        status['closed_m1_feed']=dict(policy=self.c.get('gm_closed_m1_policy'),subscribed=sorted(getattr(self,'m1_subscriptions',set())))
        status['symbol_market_policy']=self.c.get('symbol_market_policy')
        status['cycle_work']=getattr(self,'last_cycle_work',None)
        feed=getattr(self,'tq_feed',None)
        if feed is not None:
            try:
                from .paper_tq import latest_committed
                proof=latest_committed(feed.spool/'health.jsonl')
                status['external_market_feed']=dict(policy=self.c['external_market_policy'],worker_alive=feed.is_alive(),
                    run_id=proof.get('run_id'),pid=proof.get('pid'),status=proof.get('status'),updated_at=proof.get('updated_at'),contracts=proof.get('contracts'))
            except (OSError,ValueError):status['external_market_feed']=dict(status='UNKNOWN_READ_FAILURE',worker_alive=feed.is_alive())
        from .paper_clock_health import health as clock_health
        status['clock_health']=clock_health(self.c)
        monitor=getattr(self,'service_health',None)
        status['service_health']=getattr(monitor,'state',None)
        observer=getattr(self,'price_action_observer',None)
        try:status['price_action_shadow']=None if observer is None else observer.health()
        except Exception:status['price_action_shadow']=dict(status='UNKNOWN_WITH_GAP',affects_execution=False)
        status['quote_feed']=dict(policy=self.c.get('quote_policy_version'),subscribed=sorted(getattr(self,'tick_subscriptions',set())),counts=dict(getattr(self,'quote_feed_counts',{})),latest=getattr(self,'quote_feed_last',{}))
        from .paper_gm_handoff import enabled as priority
        if priority(self.c):
            from .paper_tq import external_product
            status['gm_execution_data']={cs:dict(core_alive=p['core'].thread.is_alive(),market_alive=p['market'].thread.is_alive(),
                core_ready=bool(p.get('ready') and not p.get('frozen')),fault=p.get('fault'),progress=p['core'].progress,
                pending_callbacks=len(p.get('gm_warmup_pending',{}))) for cs,p in self.products.items() if not external_product(self.c,cs)}
            status['quote_feed']['mailbox']=dict(pending=self.host_ticks.qsize(),replaced=self.host_ticks.replaced,older_rejected=self.host_ticks.older,
                note='received callback count and applied latest quote count are distinct; original clocks retained')
        gates=[]
        from .paper_symbol_health import enabled as isolated
        if ready_count!=len(self.c['universe']) and not isolated(self.c):gates.append('PRODUCTS_NOT_READY')
        if not broker or broker['status']!='RECONCILED':gates.append('ACCOUNT_NOT_RECONCILED')
        if broker and broker.get('pause_new'):gates.append('STOP_NEW_OPEN')
        if broker and broker.get('halt_reason'):gates.append(broker['halt_reason'])
        if broker and broker.get('new_entry_cooldown_reason'):gates.append(broker['new_entry_cooldown_reason'])
        if status['clock_health'].get('reason'):gates.append(status['clock_health']['reason'])
        if monitor and not (status['service_health'] or {}).get('service_facts_ready',
                (status['service_health'] or {}).get('service_ready',False)):gates.append('SERVICE_RECOVERY_PENDING')
        if self.c.get('quote_policy_version'):
            from .paper_tq import external_product
            expected_gm={s for s in self.records if not external_product(self.c,product_of(s))}
            if set(status['quote_feed']['subscribed'])!=expected_gm and not isolated(self.c):gates.append('TICK_SUBSCRIPTIONS_NOT_READY')
            status['quote_feed']['expected_gm_subscriptions']=sorted(expected_gm)
            from .paper_closed_m1 import enabled as gm_closed
            if gm_closed(self.c) and set(getattr(self,'m1_subscriptions',set()))!=expected_gm and not isolated(self.c):gates.append('CLOSED_M1_SUBSCRIPTIONS_NOT_READY')
        if status['notifications'] and not status['notifications'].get('health_fresh'):gates.append('NOTIFICATION_HEALTH_NOT_READY')
        quote_checks={}
        for symbol in self.records:
            cal=self.calendar.for_symbol(symbol)
            q,why=self.engine.valid_quote(symbol,now) if self.engine else (None,'EXECUTION_NOT_READY')
            quote_checks[symbol]=dict(session_open=cal.market_open(now),status=why if cal.market_open(now) else 'CLOSED_SESSION_NO_FRESH_QUOTE_EXPECTED')
        status['quote_eligibility']=quote_checks
        status['entry_readiness']=dict(account_enabled=not gates,reasons=gates,
            state='BLOCKED' if gates else 'READY_FOR_NATURAL_SIGNALS' if any(x['session_open'] for x in quote_checks.values()) else 'ACCOUNT_ENABLED_WAITING_SESSION',
            note='进程/预热就绪不等于允许新开；具体报价、日历及每条候选仍须实时核验')
        if isolated(self.c):
            proofs=(status.get('service_health') or {}).get('symbol_readiness',{})
            status['symbol_entry_readiness']={symbol:dict(account_gates_ready=not gates,market_proof=proofs.get(symbol),quote=quality,
                ready_for_candidate_checks=bool(not gates and proofs.get(symbol,{}).get('ready') and quality['session_open'] and quality['status']=='OK')) for symbol,quality in quote_checks.items()}
            status['entry_readiness']['state']='BLOCKED' if gates else 'ACCOUNT_READY_CHECK_EACH_SYMBOL'
            status['entry_readiness']['note']='仅共享账户门槛；行情与报价按symbol独立，仍须候选、风险、原截止及原生最终门槛；不是下单授权。'
        atomic(self.evidence/'status.json',status)
        text='# 多品种掘金模拟观察\n\n'+str(now)+'\n\n阶段：'+phase+'；就绪 '+str(ready_count)+'/'+str(len(self.c['universe']))+'；完整交易日槽 '+str(len(slots))+'/20。缺数日不移出复核分母。\n\n'
        text+='|品种|实际合约|可评估新开|预热M15|缺M1|累计信号/拦截|\n|---|---|---|---:|---:|---|\n'
        for cs,r in rows.items():text+='|'+r['name']+'|'+str(r['symbol'])+'|'+str(r['ready'])+'|'+str(r['warmup'].get('expected_m15','UNKNOWN'))+'|'+str(r['warmup'].get('missing_m1','UNKNOWN'))+'|'+json.dumps(r['counts'],ensure_ascii=False)+'|\n'
        text+='\n'
        if broker:
            positive=[p for p in broker['positions'] if int(p['volume'])>0]
            text+='模拟净值 '+str(broker['nav'])+'；账户权益变化 '+str(broker['platform_equity_change'])+'（含联调与未平估值）；平台模拟扣费 '+str(broker['platform_simulated_deductions'])+'。费用未核验。\n\n'
            text+='正持仓 '+str(len(positive))+'；活动意图 '+str(broker['active_intents'])+'；对账 '+broker['status']+'；pause_new '+str(broker['pause_new'])+'；停开来源 '+str(broker.get('pause_metadata'))+'；风险原因 '+str(broker['halt_reason'])+'。\n\n'
            text+='持仓：\n\n'+json.dumps(positive,ensure_ascii=False,indent=2)+'\n\n保护与截止：\n\n'+json.dumps(broker['position_plans'],ensure_ascii=False,indent=2)+'\n\n应平事件：\n\n'+json.dumps(broker['due_events'],ensure_ascii=False,indent=2)+'\n'
        text+='\n通知状态：'+json.dumps(status['notifications'],ensure_ascii=False)+'。HTTP独立进程；不以提醒成功替代下单、成交或平仓确认。\n'
        (self.evidence/'daily_report.md').write_text(text,encoding='utf8')
        if self.notifications and self.engine:
            if getattr(self,'started_with_pause',False) and ready_count==len(self.c['universe']) and broker.get('pause_new') and not getattr(self,'paused_start_notice',False):
                self.notice('paused-start:'+self.run_id,'⛔ 账户仍停开·策略不会下单',[
                    str(len(self.c['universe']))+'品种预热就绪，但账户 pause_new=true，技术候选只记录、不报单。',
                    '停开来源 '+str((broker.get('pause_metadata') or {}).get('reason','UNKNOWN_LEGACY_PAUSE')),
                    '必须完成单次受控恢复；后续重连或进程健康不会自动解除保护。'])
                self.paused_start_notice=True
            if not self.start_notice and all(p['ready'] or p['frozen'] for p in self.products.values()):
                self.notice('startup:'+self.run_id,str(len(self.c['universe']))+'品种运行状态',[r['name']+' '+str(r['symbol'])+'：'+('预热就绪' if r['ready'] else '暂停新开 '+str(r['fault'])) for r in rows.values()]+[
                    '账户阶段 '+phase+'；停开标志 '+str(self.engine.s['pause_new'])+'；风险原因 '+str(self.engine.s['halt_reason'] or '无'),
                    '每笔最多1手，同品种不加仓，组合最多2仓；实盘关闭。','日盘15:05发送账户摘要；已知持仓持续监督。']);self.start_notice=True
            local=now.tz_convert('Asia/Shanghai')
            if (local.hour==15 and local.minute>=5) or 15<local.hour<20:
                day=self.calendar.day(now)
                pos=[p for p in broker['positions'] if int(p['volume'])>0]
                cash=self.engine.snapshot['cash'] if self.engine.snapshot else {}
                hold_lines=[]
                trades=[x for x in self.engine.s.get('strategy_roundtrips',{}).values() if self.calendar.day(x['closed_at'])==day]
                natural_gross=sum(x['gross'] for x in trades);natural_fees=sum(x['platform_commission'] for x in trades)
                for position in pos:
                    direction='bull' if int(position['side'])==1 else 'bear';plan=broker['position_plans'].get(position['symbol']+'|'+direction,{})
                    hold_lines.append(position['symbol']+' '+DIRECTIONS[direction]+' '+str(position['volume'])+'手；入场 '+str(position['vwap'])+'；止损 '+str(plan.get('stop','UNKNOWN'))+'；目标 '+str(plan.get('target','UNKNOWN'))+'；截止 '+str(plan.get('deadline','UNKNOWN')))
                self.notice('daily:'+day,'日盘收盘账户摘要',['交易日 '+day,'快照时间 '+str(now),'模拟净值 '+str(broker['nav']),
                    '账户权益变化 '+str(broker['platform_equity_change'])+'（含联调／未平估值）','平台模拟扣费 '+str(broker['platform_simulated_deductions']),
                    '正持仓 '+str(len([p for p in broker['positions'] if int(p['volume'])>0]))+'；活动意图 '+str(broker['active_intents']),
                    '数据就绪 '+str(ready_count)+'/'+str(len(self.c['universe']))+'；完整日槽 '+str(len(slots))+'/20',
                    'D+1及休市前清仓持续管理；模拟费用与真实费用分开。'],
                    fields=[('交易日',day),('账户环境','专用GM期货模拟账户'),('模拟净值',str(broker['nav'])),('可用资金',str(cash.get('available','UNKNOWN'))),
                        ('账户权益变化',str(broker['platform_equity_change'])+'（含联调和未平估值）'),('平台模拟扣费',str(broker['platform_simulated_deductions'])),
                        ('当前正持仓',str(len(pos))+'，组合上限2'),('活动委托意图',str(broker['active_intents'])),
                        ('本交易日自然策略已平',str(len(trades))+'笔；联调排除'),('策略已平毛损益',number(natural_gross)+' 元'),
                        ('策略平台扣费 / 扣后额',number(natural_fees)+' / '+number(natural_gross-natural_fees)+' 元；非真实净收益'),
                        ('连续亏损 / 冷却',str(broker.get('loss_cooldown',{}).get('consecutive_losses',0))+'笔 / '+str(broker.get('new_entry_cooldown_reason') or '无'))],
                    reasons=['数据就绪 '+str(ready_count)+'/'+str(len(self.c['universe']))+'；完整交易日槽 '+str(len(slots))+'/20',
                        '已平测试与策略分别记账，权益变化不冒充已核验真实净收益。']+(hold_lines or ['当前无正持仓。']),
                    execution=['快照 '+str(now),'对账 '+broker['status']+'；停开原因 '+str(broker['halt_reason'] or '无'),
                        '夜盘及已知敞口继续监督；缺数日保留在第5/20日复核分母。'])
        return status

    def supervise_notification_sender(self,now):
        if self.readonly or self.c.get('notification_policy_version')!=CARD_POLICY or not self.engine:return
        if (stamp(now)-stamp(self.started)).total_seconds()<=60:return
        h=self.notifications.health() if self.notifications else {}
        problem='FEISHU_SENDER_EXITED' if not h.get('worker_alive') else 'FEISHU_SENDER_HEALTH_STALE' if not h.get('health_fresh') else None
        if problem:
            self.engine.pause_entries(problem,now,'NOTIFICATION_SENDER')
            if getattr(self,'notification_sender_problem',None)!=problem:self.event('NOTIFICATION_HEALTH_FAULT',reason=problem)
        self.notification_sender_problem=problem

    def supervise_trend_notifier(self,now,ready_count):
        """Owned notification child; one startup, failure stops entries, no restart."""
        if self.readonly or self.c.get('strategy_name')!='S0_SHORT_PAPER_CORE_V2':return
        import subprocess,sys
        process=getattr(self,'trend_notifier',None)
        if process is None:
            from .paper_symbol_health import enabled as isolated
            if (ready_count==0 if isolated(self.c) else ready_count!=len(self.c['universe'])):return
            root=Path(self.c['root'])
            if root.name!='_strategy_runtime':
                self.engine.s['pause_new']=True;self.engine.save()
                return  # Client root must be explicit; no accidental project fallback.
            env=dict(os.environ);env.pop('GM_TOKEN',None)
            stdout=(self.evidence/'trend_owned.stdout.local.log').open('a',encoding='utf8')
            stderr=(self.evidence/'trend_owned.stderr.local.log').open('a',encoding='utf8')
            self.trend_notifier=subprocess.Popen([sys.executable,'-X','utf8',str(root/'tools/paper_trend_notify.py'),
                '--client',str(root.parent)],cwd=root.parent,env=env,stdout=stdout,stderr=stderr,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            stdout.close();stderr.close();self.trend_notifier_started=stamp(now)
            self.event('TREND_NOTIFIER_STARTED',pid=self.trend_notifier.pid,owned_by_host=True)
            return
        problem=None
        diagnostic={}
        if process.poll() is not None:problem='TREND_NOTIFIER_EXITED:'+str(process.returncode)
        elif (stamp(now)-self.trend_notifier_started).total_seconds()>60:
            health=self.evidence/'trend_reminder_health.local.json'
            try:
                from .paper_progress import progress_problem
                h=read_json(health)
                # A child can publish after this coordinator cycle began.
                # Judge that proof at the actual post-read clock, never the
                # older cycle/report timestamp. Real future/stale proofs still fail.
                checked_at=pd.Timestamp.now(tz='Asia/Shanghai')
                problem=progress_problem(h,process.pid,checked_at)
                diagnostic=dict(report_cycle_at=str(now),checked_at=str(checked_at),proof_at=h.get('at'),
                    proof_age_seconds=(checked_at-stamp(h['at'])).total_seconds(),pid=h.get('pid'),
                    expected_pid=process.pid,stage=h.get('stage'),stage_started_at=h.get('stage_started_at'),
                    status=h.get('status'),stage_failures=h.get('stage_failures'))
            except (OSError,ValueError,KeyError,TypeError) as exc:
                problem='TREND_NOTIFIER_HEALTH_UNKNOWN'
                diagnostic=dict(report_cycle_at=str(now),checked_at=pd.Timestamp.now(tz='Asia/Shanghai').isoformat(),
                    read_failure=getattr(exc,'details',None) or dict(category=type(exc).__name__))
        if problem:
            self.engine.pause_entries(problem,now,'TREND_NOTIFIER')
            if getattr(self,'trend_notifier_problem',None)!=problem:
                self.event('NOTIFICATION_HEALTH_FAULT',reason=problem,diagnostic=diagnostic)
                atomic(self.evidence/('trend_failure_'+self.run_id+'_'+stamp(now).strftime('%H%M%S%f')+'.local.json'),
                       dict(run_id=self.run_id,reason=problem,diagnostic=diagnostic))
                self.notice('trend-owned-fault:'+self.run_id,'趋势提醒异常，暂停新开',[problem,'持仓退出监督继续；不自动重启或解除保护。'])
            self.trend_notifier_problem=problem
        elif getattr(self,'trend_notifier_problem',None):
            self.event('NOTIFICATION_HEALTH_RECOVERED',prior=self.trend_notifier_problem,
                       pause_retained=self.engine.s['pause_new'])
            self.trend_notifier_problem=None

    def restore_strategy_fills(self):
        """Closed retired fills remain in the ledger, never enter a new product core."""
        for i in self.engine.s['intents'].values():
            if not (i['effect']==1 and i.get('filled') and i['tag']=='STRATEGY' and i['id'] not in self.fill_notified):continue
            if i['csymbol'] not in self.products:
                remaining=sum((1 if x['effect']==1 else -1)*int(x.get('filled',0)) for x in self.engine.s['intents'].values()
                              if x['symbol']==i['symbol'] and x['direction']==i['direction'])
                exposed=any(p['symbol']==i['symbol'] and int(p.get('volume',0))>0 for p in self.engine.positions())
                if remaining or exposed or self.engine.active(i['symbol']):
                    self.engine.s['pause_new']=True;self.engine.save()
                    self.event('RETIRED_PRODUCT_EXPOSURE',csymbol=i['csymbol'],remaining=remaining)
                    raise ValueError('retired product still has exposure or active intent')
                self.event('RETIRED_CLOSED_FILL_RETAINED',csymbol=i['csymbol'],intent_id=i['id'])
            else:self.products[i['csymbol']]['core'].inbox.put(('FILL',dict(i)))
            self.fill_notified.add(i['id'])

    def apply_transport(self):
        applied=0
        for job,reply in self.transport.drain():
            if self.readonly:
                if reply.get('ok') and job['op']=='snapshot':
                    s=reply['data'];self.verified=all(r.get('account_id')==self.c['account_id'] for r in [s['cash']]+s['positions']+s['orders']+s['active']+s['fills']);self.readonly_snapshot=s
            elif self.engine:self.engine.accept(job,reply,pd.Timestamp.now(tz='Asia/Shanghai'))
            applied+=1
        return applied

    def cycle(self):
        """One bounded execution cycle, shared by CLI and official GM timer."""
        now=pd.Timestamp.now(tz='Asia/Shanghai');mono=time.monotonic();self.max_gap=max(self.max_gap,mono-self.last_loop);self.last_loop=mono
        from .paper_cycle_work import CycleWork
        work=CycleWork();self._cycle_work=work
        for cs,p in self.products.items():
            if not p['market'].thread.is_alive() or not p['core'].thread.is_alive():
                if p['fault']!='WORKER_EXITED':self.notice('worker:'+self.run_id+':'+cs,'单品种进程异常',[PRODUCTS[cs]['name'],'已停止新开；已知持仓继续由账户执行器监督。'])
                p.update(frozen=True,fault='WORKER_EXITED')
        feed=getattr(self,'tq_feed',None)
        if feed is not None and not feed.is_alive():
            from .paper_tq import TQ_ADDED
            for cs in TQ_ADDED:self.products[cs].update(frozen=True,fault='TQ_DATA_SIDECAR_EXITED')
        if not self.quote_worker.is_alive() and self.engine:
            from .paper_symbol_health import enabled as isolated
            if not isolated(self.c):self.engine.s['pause_new']=True
            self.notice('quote-worker:'+self.run_id,'备用报价进程异常',
                ['备用GM轮询不可用；新政策按各品种真实主行情和原报价门槛核验，其它源独立。' if isolated(self.c) else '停止账户新开；若有余仓，保留风险并等待有效报价和受控恢复。'])
        work.run('RPC_APPLY_FIRST',self.apply_transport)
        if self.engine and self.c.get('execution_policy_version')=='PAPER_EXEC_V5':
            read_fault=not self.transport.reader_available() or self.transport.reader.hung()
            if read_fault:
                self.engine.ready=False;self.engine.block='READ_COLLECTOR_UNAVAILABLE_OR_STALE'
                if not getattr(self,'reader_fault_seen',False):
                    self.event('READ_COLLECTOR_FAULT_WRITER_RETAINED')
                    if not getattr(self,'service_health',None):self.notice('reader:'+self.run_id,'只读采集通道异常',['停止新开；唯一写入者保留，退出义务使用新鲜最小持仓事实继续监督；不自动创建第二写入者。'])
            self.reader_fault_seen=read_fault
        if hasattr(self,'c'):work.run('CLOCK_PROOF',lambda:self.supervise_clock(pd.Timestamp.now(tz='Asia/Shanghai')))
        try:
            from .paper_symbol_health import enabled as isolated
            if isolated(self.c):
                from .paper_gm_handoff import enabled as priority,ordered_products
                order=ordered_products(self) if priority(self.c) else list(self.products)
                if priority(self.c):work.run('GM_FEEDS_FIRST',lambda:self.market_events(now,csymbol='__FEEDS_ONLY__'))
                for cs in order:
                    for stage,method in [('MARKET_HANDOFF',self.market_events),('CORE_HANDOFF',self.core_events)]:
                        try:work.run(stage,lambda method=method,cs=cs:method(now,csymbol=cs))
                        except Exception as exc:FiveRunner.freeze_product(self,cs,'PRODUCT_HANDOFF_FAILED',now,type(exc).__name__+':'+str(exc)[:120])
                work.run('MARKET_HANDOFF',lambda:self.market_events(now,csymbol='__FEEDS_ONLY__'))
            else:
                work.run('MARKET_HANDOFF',lambda:self.market_events(now))
                work.run('CORE_HANDOFF',lambda:self.core_events(now))
        except Exception as exc:
            self.event('INTERNAL_PRODUCT_HANDOFF_ERROR',category=type(exc).__name__,detail=str(exc)[:120])
            for p in self.products.values():p['frozen']=True
        # A reply arriving during queue work is applied before the risk pass.
        work.run('RPC_APPLY_AFTER_HANDOFF',self.apply_transport)
        if self.engine:
            control=Path(self.c['state_dir'])/'control.json'
            if control.exists():
                c=read_json(control)
                if c.get('stop_new_open'):self.engine.s['pause_new']=True
                if c.get('flatten'):
                    for m in self.engine.s['meta'].values():m['force_exit']='USER_CONTROL_FLATTEN'
            try:work.run('RISK_TICK',lambda:self.engine.tick(pd.Timestamp.now(tz='Asia/Shanghai')))
            except Exception as exc:
                self.engine.ready=False;self.engine.block='EXECUTION_FAULT';self.engine.s['pause_new']=True
                self.event('EXECUTION_FAULT',category=type(exc).__name__)
            for cs,p in self.products.items():
                if (p.get('frozen') or not p.get('ready')) and p.get('metadata'):
                    self.engine.invalidate_candidates(p['metadata']['symbol'],'PRODUCT_NOT_READY',now)
            if getattr(self,'service_health',None):work.run('SERVICE_FACTS',lambda:self.service_health.update(pd.Timestamp.now(tz='Asia/Shanghai')))
            work.run('CANDIDATE_DRAIN',lambda:self.engine.drain_candidates(pd.Timestamp.now(tz='Asia/Shanghai')))
            for cs,p in self.products.items():
                symbol=None if p['metadata'] is None else p['metadata']['symbol']
                positions=dict(Counter('bull' if int(r['side'])==1 else 'bear' for r in self.engine.positions() if int(r['volume'])>0 and r['symbol']==symbol))
                if positions!=p['last_positions']:p['core'].inbox.put(('POSITIONS',positions));p['last_positions']=positions
            self.restore_strategy_fills()
            work.run('NOTIFICATION_HANDOFF',self.pump_notifications)
            problem=self.engine.s['halt_reason'] or self.engine.summary().get('new_entry_cooldown_reason') or ('PENDING_RECON' if self.transport.hung() else None)
            if problem and problem!=self.last_problem:self.notice('risk:'+self.run_id+':'+problem,'账户风险或执行异常',[problem,'暂停新开；已知敞口继续核对与减仓监督。'])
            self.last_problem=problem
        # Entries/exits/cancels have already had first use of the serial RPC.
        # A fresh applied snapshot is reusable; do not monopolize the channel.
        if self.engine and self.c.get('execution_policy_version')=='PAPER_EXEC_V5' and self.engine.ready and not getattr(self,'writer_context_checked',False) and not self.transport.busy():
            # One actual read on the writer after the independent reader has
            # reconciled: prove account context survives the second connection.
            self.transport.request('exit_snapshot');self.writer_context_checked=True
        snapshot_age=None if not self.engine or not self.engine.last_snapshot else (stamp(now)-stamp(self.engine.last_snapshot)).total_seconds()
        interval=2 if self.calendar.contracts and self.calendar.market_open(now) else 5
        urgent=self.engine and (not self.engine.ready or snapshot_age is None or snapshot_age>2)
        if self.calendar.contracts and not self.transport.busy() and (urgent or mono-self.last_poll>=interval):
            self.transport.request('snapshot');self.last_poll=mono
        if mono-self.last_report>=5:
            self.last_cycle_work=work.snapshot()
            try:status=work.run('REPORT',lambda:self.report(pd.Timestamp.now(tz='Asia/Shanghai')))
            except Exception as exc:
                if self.engine:self.engine.s['pause_new']=True
                self.event('REPORT_FAULT',category=type(exc).__name__);status={}
            self.last_report=mono
            # Observe/report work cannot leave an already-returned reply queued
            # until another timer. Freshness is checked at actual application.
            work.run('RPC_APPLY_AFTER_REPORT',self.apply_transport)
            self.last_cycle_work=work.snapshot()
            atomic(self.evidence/'cycle_work.local.json',self.last_cycle_work)
            if self.readonly and status.get('phase')=='READONLY_PREFLIGHT_PASS':print(json.dumps(dict(phase=status['phase'],ready_products=len(self.c['universe']),status_file=str(self.evidence/'status.json'))),flush=True);return True
        self.last_cycle_work=work.snapshot()
        return False

    def run(self,max_seconds=0):
        started=time.monotonic();self.last_loop=started
        while True:
            if self.cycle():return
            if max_seconds and time.monotonic()-started>=max_seconds:return
            time.sleep(.2)


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--preflight',action='store_true');p.add_argument('--max-seconds',type=int,default=0)
    a=p.parse_args()
    if a.max_seconds and not a.preflight:p.error('timed shutdown is readonly only')
    runner=FiveRunner(a.config,a.preflight)
    print(json.dumps(dict(pid=os.getpid(),readonly=a.preflight,evidence_dir=str(runner.evidence))),flush=True);runner.run(a.max_seconds)
