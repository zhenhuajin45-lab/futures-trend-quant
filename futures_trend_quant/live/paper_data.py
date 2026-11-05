"""GM-only paper-purpose data worker. No broker functions are imported here."""
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
import hashlib
import json
import multiprocessing
import os
import queue
import threading
import time
import pandas as pd
from futures_trend_quant.core.models import Bar
from futures_trend_quant.core.pipeline import CoreStepInput,StrategyRuntimeBook,run_core_step
from futures_trend_quant.core.state_store import StrategyRuntimeStore
from futures_trend_quant.data.gm_bars import bars_from_records
from backtest.aggregation import combine_bars
from .paper_calendar import PaperCalendar
from .paper_broker import atomic
from .paper_products import PRODUCTS,validate_actual

FIELDS='symbol,frequency,bob,eob,open,high,low,close,volume,amount,position'
CORE_MIN_M15=251  # Existing analyze_market contract, not a new strategy parameter.


def restore_fill(runtime,csymbol,intent):
    """Replayed persisted fills consume evidence without rewinding the cooldown."""
    evidence=intent.get('core_evidence_id',intent['evidence_id'])
    if evidence in runtime.used_evidence_ids:return
    key=csymbol+'|'+intent['direction'];index=int(intent['signal_m15_index'])
    if index<runtime.last_fill_indices.get(key,-1):
        runtime.used_evidence_ids.add(evidence);return
    runtime.record_fill(csymbol=csymbol,direction=intent['direction'],price=intent['filled_vwap'],evidence_id=evidence,m15_index=index)


def complete_bars(bars,period):
    grouped={}
    for b in bars:
        second=int(b.eob.timestamp());width=period*60
        end=((second+width-1)//width)*width
        grouped.setdefault((b.symbol,end),[]).append(b)
    complete=[]
    for (_,end),items in sorted(grouped.items(),key=lambda x:x[0][1]):
        if len(items)!=period:continue
        items=sorted(items,key=lambda b:b.eob)
        if any(b.eob.timestamp()!=end-(period-1-j)*60 for j,b in enumerate(items)):continue
        complete.append(combine_bars(items,str(period*60)+'s'))
    return complete


def _market_process(config,events):
    worker=MarketWorker(config,child=True)
    worker.events=events
    worker._run()


def pinned_contract(config,mapping):
    pin=Path(config['state_dir'])/'contract_pin.json'
    if pin.exists():
        saved=json.loads(pin.read_text(encoding='utf8'))
        if saved['config_sha256']!=config['config_sha256']:raise ValueError('contract pin/config mismatch')
        symbol=saved['symbol']
    else:
        prior=Path(config['state_dir'])/'execution.json'
        known=set()
        if prior.exists():
            state=json.loads(prior.read_text(encoding='utf8'))
            if state['config_sha256']!=config['config_sha256']:raise ValueError('execution/config mismatch before market restore')
            known={i['symbol'] for i in state['intents'].values()}
        if len(known)>1:raise ValueError('multiple old contracts require supervised restore')
        symbol=next(iter(known)) if known else config.get('fixed_actual_symbol') or (mapping[-1]['symbol'] if mapping else None)
        if not symbol:raise ValueError('no known actual contract')
        atomic(pin,dict(symbol=symbol,config_sha256=config['config_sha256']))
    validate_actual(config.get('csymbol','DCE.M'),symbol)
    return symbol


class MarketWorker:
    def __init__(self,config,child=False):
        self.c=config;self.events=queue.Queue() if child else multiprocessing.get_context('spawn').Queue()
        self.started=datetime.now(timezone.utc).isoformat();self.stop=False
        # Native GM calls may retain the GIL. A thread cannot isolate the risk timer.
        self.thread=None if child else multiprocessing.get_context('spawn').Process(target=_market_process,args=(config,self.events),name='paper-gm-market',daemon=True)
        self.raw={};self.symbol=None;self.calendar=None;self.failure=None
        self.evidence=Path(config['evidence_dir']);self.evidence.mkdir(parents=True,exist_ok=True)

    def seen(self,kind,**fields):
        with (self.evidence/'seen_data.jsonl').open('a',encoding='utf8') as f:
            f.write(json.dumps(dict(requested_at=datetime.now(timezone.utc).isoformat(),purpose='PAPER_OBSERVATION_SEEN',
                original_formal90_status='ORIGINAL_WHOLE_POOL_WINDOW_NO_LONGER_UNSEEN',independent_score_run=False,kind=kind,**fields),default=str)+'\n')

    def bars(self,frame,cutoff):
        if frame is None or frame.empty:raise ValueError('M1 unavailable')
        f=frame.copy();f['eob']=pd.to_datetime(f.eob,utc=True).dt.tz_convert('Asia/Shanghai')
        f['bob']=pd.to_datetime(f.bob,utc=True).dt.tz_convert('Asia/Shanghai')
        f=f.loc[f.eob<=pd.Timestamp(cutoff).floor('min')].sort_values('eob')
        if f.empty or f.eob.duplicated().any() or set(f.symbol)!={self.symbol}:raise ValueError('invalid M1 identity/clock')
        if f.eob.min()<pd.Timestamp('2026-05-08',tz='Asia/Shanghai'):raise ValueError('paper history outside authorized recent prewarm')
        if any((f.eob-f.bob)!=pd.Timedelta(minutes=1)):raise ValueError('M1 bob/eob mismatch')
        return bars_from_records(f.to_dict('records'))

    def _run(self):
        try:
            from gm.api import set_token,current,history_n,fut_get_continuous_contracts,get_instrumentinfos,get_trading_session,get_trading_dates,ADJUST_NONE
            set_token(os.environ['GM_TOKEN'])
            now=pd.Timestamp.now(tz='Asia/Shanghai')
            cs=self.c.get('csymbol','DCE.M');exchange=PRODUCTS[cs]['exchange']
            self.seen('EXCHANGE_CALENDAR_MAIN_MAPPING_AND_SPEC',csymbol=cs,exchange=exchange)
            dates=get_trading_dates(exchange=exchange,start_date='2026-05-08',end_date='2026-12-31')
            d=now.date().isoformat();future=[str(x)[:10] for x in dates if str(x)[:10]>(d if now.hour>=20 else (now-pd.Timedelta(days=1)).date().isoformat())]
            if not future:raise ValueError('exchange calendar unavailable')
            day=future[0];mapping=fut_get_continuous_contracts(csymbol=cs,start_date=day,end_date=day)
            self.symbol=pinned_contract(self.c,mapping)
            if not mapping or self.symbol!=mapping[-1]['symbol']:self.events.put(('ROLLOVER_FREEZE',dict(previous=self.symbol,current=mapping[-1]['symbol'] if mapping else None)))
            validate_actual(cs,self.symbol)
            sessions=get_trading_session(symbols=self.symbol,df=False)[0]['time_trading']
            spec=get_instrumentinfos(symbols=self.symbol,fields='symbol,price_tick,multiplier,margin_ratio',df=False)[0]
            self.calendar=PaperCalendar(dates,sessions,cs)
            metadata=dict(csymbol=cs,symbol=self.symbol,spec=spec,mapping=mapping,dates=dates,sessions=sessions,source='GM')
            atomic(self.evidence/'market_metadata.local.json',metadata)
            self.events.put(('METADATA',metadata,self.calendar))
            self.seen('RECENT_ACTUAL_M1_WARMUP',symbol=self.symbol,count=self.c['seed_m1_count'],end_time=str(now),use='STATE_ONLY_NO_OLD_PNL')
            frame=history_n(symbol=self.symbol,frequency='60s',count=self.c['seed_m1_count'],end_time=now.to_pydatetime(),fields=FIELDS,skip_suspended=False,adjust=ADJUST_NONE,df=True)
            raw_frame=frame.copy();normalization=[]
            if self.c.get('warmup_no_trade_manifest'):
                from .paper_warmup import normalize_no_trade_minutes
                frame,normalization=normalize_no_trade_minutes(frame,Path(self.c['root'])/self.c['warmup_no_trade_manifest'],self.c['root'],self.symbol,now)
            initial=self.bars(frame,now)
            cache=Path(self.c['root'])/'data/cache/paper_market'/(cs.replace('.','_')+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'));cache.mkdir(parents=True,exist_ok=False)
            frame.to_parquet(cache/'initial_m1.parquet',index=False)
            raw_frame.to_parquet(cache/'gm_original_m1.parquet',index=False)
            atomic(cache/'input_manifest.json',dict(symbol=self.symbol,rows=len(frame),sha256=hashlib.sha256((cache/'initial_m1.parquet').read_bytes()).hexdigest(),purpose='PAPER_WARMUP_STATE_ONLY',requested_at=str(now),
                gm_original_sha256=hashlib.sha256((cache/'gm_original_m1.parquet').read_bytes()).hexdigest(),zero_trade_normalization=normalization))
            from .paper_runner import warmup_coverage
            coverage=warmup_coverage(initial,self.calendar,self.c['warmup_m15'])
            self.raw={b.identity:b for b in initial}
            # Avoid unpickling 22,000 pandas timestamps in the risk process.
            self.events.put(('INITIAL_CACHE',dict(path=str(cache/'initial_m1.parquet'),
                sha256=hashlib.sha256((cache/'initial_m1.parquet').read_bytes()).hexdigest(),
                symbol=self.symbol,cutoff=str(now)),coverage))
            last_history=0.;last_quote=0.;last_mapping_day=day
            while not self.stop:
                now=pd.Timestamp.now(tz='Asia/Shanghai')
                # No subscription switches: no held contract can lose its feed to make room for a new main.
                target=self.calendar.day(now)
                if target!=last_mapping_day:
                    self.seen('DAILY_MAPPING_RECHECK',symbol=self.symbol,trading_date=target)
                    m=fut_get_continuous_contracts(csymbol=cs,start_date=target,end_date=target)
                    if not m or m[-1]['symbol']!=self.symbol:self.events.put(('ROLLOVER_FREEZE',dict(previous=self.symbol,current=None if not m else m[-1]['symbol'])))
                    last_mapping_day=target
                market_open=self.calendar.market_open(now)
                if self.c.get('quote_poll_enabled',True) and (market_open or time.monotonic()-last_quote>30):
                    self.seen('CURRENT_SNAPSHOT',symbol=self.symbol)
                    rows=current(symbols=self.symbol)
                    received=pd.Timestamp.now(tz='Asia/Shanghai')
                    if rows:
                        q=dict(rows[0]);book=q.get('quotes') or []
                        quote=dict(created_at=str(q['created_at']),last=q.get('price'),tick=float(spec['price_tick']),
                            bid=book[0].get('bid_p',0) if book else 0,ask=book[0].get('ask_p',0) if book else 0,
                            bid_size=book[0].get('bid_v',0) if book else 0,ask_size=book[0].get('ask_v',0) if book else 0,
                            source='GM_CURRENT_POLL_NOT_STREAM')
                        self.events.put(('QUOTE',self.symbol,quote,received.isoformat()))
                    last_quote=time.monotonic()
                last_expected=self.calendar.clock[self.calendar.clock<=now]
                close_grace=bool(len(last_expected) and (now-last_expected[-1]).total_seconds()<=65)
                if time.monotonic()-last_history>=5 and (market_open or close_grace):
                    self.seen('CLOSED_M1_POLL',symbol=self.symbol,count=15)
                    latest=history_n(symbol=self.symbol,frequency='60s',count=15,end_time=now.to_pydatetime(),fields=FIELDS,skip_suspended=False,adjust=ADJUST_NONE,df=True)
                    added=[]
                    for b in self.bars(latest,now):
                        old=self.raw.get(b.identity)
                        if old is not None and old!=b:raise ValueError('closed M1 revision; freeze entries before recomputation')
                        if old is None:
                            self.raw[b.identity]=b;added.append(b)
                    if added:
                        previous=max(x.eob for x in initial)
                        expected=self.calendar.clock[(self.calendar.clock>pd.Timestamp(previous))&(self.calendar.clock<=pd.Timestamp(added[-1].eob))]
                        if set(pd.DatetimeIndex([x.eob for x in added]))!=set(expected):
                            self.events.put(('DATA_GAP',dict(expected=len(expected),received=len(added))))
                        self.events.put(('NEW_BARS',added));initial=[added[-1]]
                    last_history=time.monotonic()
                time.sleep(1 if market_open else 5)
        except Exception as exc:
            self.failure=type(exc).__name__+': '+str(exc)[:180];self.events.put(('DATA_FAILED',self.failure))


def _signal_process(config,inbox,outbox):
    worker=SignalWorker(config,child=True);worker.inbox=inbox;worker.outbox=outbox;worker._run()


class SignalWorker:
    def __init__(self,config,child=False):
        self.c=config;ctx=multiprocessing.get_context('spawn')
        self.inbox=queue.Queue() if child else ctx.Queue();self.outbox=queue.Queue() if child else ctx.Queue()
        self.bars=[];self.stop=False;self.positions={}
        self.checkpoint=Path(config['state_dir'])/'core_checkpoint.json'
        saved=json.loads(self.checkpoint.read_text(encoding='utf8')) if self.checkpoint.exists() else {}
        self.runtime=StrategyRuntimeBook.from_dict(saved['runtime']) if saved else StrategyRuntimeBook()
        self.last=pd.Timestamp(saved['last_eob']) if saved.get('last_eob') else None
        self.progress=dict(status='WAITING_FOR_M1',processed=0,warmup=False)
        self.thread=None if child else ctx.Process(target=_signal_process,args=(config,self.inbox,self.outbox),name='paper-s0-core',daemon=True)

    def _run(self):
        try:
            while not self.stop:
                job=self.inbox.get();kind=job[0]
                if kind=='POSITIONS':self.positions=job[1];continue
                if kind=='FILL':
                    i=job[1]
                    restore_fill(self.runtime,self.c.get('csymbol','DCE.M'),i);continue
                initial=kind in ('INITIAL','INITIAL_CACHE')
                if kind=='INITIAL_CACHE':
                    raw=job[1];path=Path(raw['path']).resolve()
                    if not path.is_relative_to(Path(self.c['root']).resolve()/'data/cache/paper_market') or hashlib.sha256(path.read_bytes()).hexdigest()!=raw['sha256']:
                        raise ValueError('initial market cache identity changed')
                    loader=MarketWorker(self.c,child=True);loader.symbol=raw['symbol']
                    bars=loader.bars(pd.read_parquet(path),raw['cutoff'])
                else:bars=job[1]
                self.bars=sorted({b.identity:b for b in self.bars+bars}.values(),key=lambda b:b.eob)[-24000:]
                m5=complete_bars(self.bars,5);m15=complete_bars(self.bars,15)
                if len(m15)<self.c['warmup_m15']:
                    self.progress=dict(status='WARMUP_INCOMPLETE',m1=len(self.bars),m15=len(m15),warmup=False)
                    self.outbox.put(('WARMUP_FAILED',self.progress));continue
                self.progress=dict(status='REBUILDING_S0' if initial else 'RUNNING',m1=len(self.bars),m15=len(m15),processed=0,warmup=False)
                for index,bar in enumerate(m15):
                    if index+1<CORE_MIN_M15 or (self.last is not None and pd.Timestamp(bar.eob)<=self.last):continue
                    cutoff=bar.eob
                    result=run_core_step(CoreStepInput(csymbol=self.c.get('csymbol','DCE.M'),symbol=bar.symbol,
                        m15_bars=tuple(m15[max(0,index+1-1200):index+1]),
                        m5_bars=tuple([b for b in m5 if b.eob<=cutoff][-1200:]),
                        m1_bars=tuple([b for b in self.bars if b.eob<=cutoff][-1200:]),
                        rules=self.c['base_config'].raw['rules'],positions_by_direction={} if initial else self.positions),self.runtime)
                    self.last=pd.Timestamp(cutoff);self.progress['processed']+=1
                    time.sleep(.001)  # Cooperative warmup: do not starve the independent risk timer.
                    if not initial:self.outbox.put(('CORE_RESULT',result,bar,datetime.now(timezone.utc).isoformat()))
                    if not initial or index%50==0:
                        atomic(self.checkpoint,dict(last_eob=self.last.isoformat(),runtime=self.runtime.to_dict()))
                        self.outbox.put(('CORE_PROGRESS',dict(self.progress)))
                atomic(self.checkpoint,dict(last_eob=self.last.isoformat(),runtime=self.runtime.to_dict()))
                # Full 1200-M15 coverage is qualified by the market calendar in the coordinator.
                from futures_trend_quant.core.facts.indicators import calculate_indicators
                from futures_trend_quant.core.facts.pipeline import finalized_entry_h1
                _,h1_end=finalized_entry_h1(m15[-1200:],self.c['base_config'].raw['rules'])
                self.progress.update(status='RUNNING',warmup=True,last_eob=self.last.isoformat(),
                    last_closed_m1=str(self.bars[-1].eob),last_closed_m5=str(m5[-1].eob),
                    last_closed_m15=str(m15[-1].eob),last_finalized_h1=None if h1_end is None else str(h1_end),
                    h1_source='UNCHANGED_CORE_AGGREGATED_CLOSED_M15; FINALIZED_ENTRY_H1',
                    m15_index=self.runtime.m15_index_for(self.c.get('csymbol','DCE.M')),
                    atr14=calculate_indicators(m15[-1200:]).atr14[-1])
                self.outbox.put(('CORE_READY',dict(self.progress),m15[-1],result if 'result' in locals() else None))
        except Exception as exc:
            self.progress=dict(status='CORE_FAILED',error=type(exc).__name__,detail=str(exc)[:180],warmup=False)
            self.outbox.put(('CORE_FAILED',self.progress))
