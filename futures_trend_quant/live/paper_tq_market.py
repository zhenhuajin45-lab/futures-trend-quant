"""Host-side external-feed consumer. Historical state is TQ-only, never mixed."""
from pathlib import Path
import json
import os
import subprocess
import time
import math
from datetime import datetime,timezone
import pandas as pd
from .paper_tq import POLICY,SOURCE,clock,load_manifest,validate_envelope,sha,atomic,latest_committed
from .paper_io import read_json,JsonlTail
from .paper_calendar import PaperCalendar
from futures_trend_quant.data.gm_bars import bars_from_records


class OwnedFeed:
    def __init__(self,c,config_path,run_id):
        self.c=c;self.run_id=run_id;self.process=None;self.logs=[]
        self.spool=Path(c['evidence_dir'])/'tq_feed'/run_id
        self.config_path=config_path

    def start(self):
        env={k:v for k,v in os.environ.items() if k not in ('GM_TOKEN','GM_STRATEGY_ID','TQ_USER','TQ_PASSWORD','TQ_AUTH_FILE','PYTHONPATH','PYTHONHOME')}
        root=Path(self.c['root']);self.spool.parent.mkdir(parents=True,exist_ok=True)
        for suffix in ('stdout','stderr'):
            self.logs.append((self.spool.parent/(self.run_id+'.'+suffix+'.local.log')).open('x',encoding='utf8'))
        self.process=subprocess.Popen([self.c['tq_feed']['python'],'-X','utf8','-m','futures_trend_quant.live.paper_tq_feed',
            '--config',str(self.config_path),'--run-id',self.run_id,'--spool',str(self.spool)],cwd=root,env=env,
            stdout=self.logs[0],stderr=self.logs[1],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

    def is_alive(self):return bool(self.process and self.process.poll() is None)
    def terminate(self):
        if not self.is_alive():return
        atomic(self.spool/'stop.request.json',dict(run_id=self.run_id,config_sha256=self.c['config_sha256']))
        try:self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Windows venv python.exe is a launcher. Terminating it alone can
            # leave the owned interpreter publishing after strategy shutdown.
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(self.process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,check=False)
            else:self.process.terminate()
    def join(self,timeout=None):
        if self.process:
            try:self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:pass
        if not self.is_alive():
            for f in self.logs:f.close()


def tq_rows(payload,symbol):
    """Do not promote the forming bar or invent a historical receive clock."""
    forming=int(payload['forming_bob_ns']);rows=[]
    previous=0
    for raw in payload['rows']:
        bob=int(raw['datetime'])
        if bob<=previous or bob>=forming or bob%60_000_000_000:raise ValueError('TQ_BAR_CLOCK_OR_ORDER_INVALID')
        if any(not math.isfinite(float(raw[k])) or float(raw[k])<0 for k in ('volume','close_oi')):raise ValueError('TQ_BAR_VOLUME_OR_OI_UNKNOWN')
        previous=bob;start=pd.Timestamp(bob,tz='UTC').tz_convert('Asia/Shanghai')
        rows.append(dict(symbol=symbol,frequency='60s',bob=start.to_pydatetime(),eob=(start+pd.Timedelta(minutes=1)).to_pydatetime(),
            **{k:raw[k] for k in ('open','high','low','close','volume')},position=raw['close_oi']))
    return bars_from_records(rows)


def comparable(bar):return tuple(getattr(bar,k) for k in ('open','high','low','close','volume','position'))


def merge_same_source(seed,incoming,calendar):
    known={b.identity:b for b in seed};added=[]
    for b in incoming:
        if not calendar.section_for_bar(b):raise ValueError('TQ_BAR_OUTSIDE_CALENDAR')
        old=known.get(b.identity)
        if old is not None:
            if comparable(old)!=comparable(b):raise ValueError('TQ_CLOSED_M1_REVISION')
        else:
            if seed and b.eob<=seed[-1].eob:raise ValueError('TQ_LATE_HISTORICAL_BAR')
            added.append(b);known[b.identity]=b
    if seed and added:
        expected=calendar.clock[(calendar.clock>pd.Timestamp(seed[-1].eob))&(calendar.clock<=pd.Timestamp(added[-1].eob))]
        if set(pd.DatetimeIndex([b.eob for b in added]))!=set(expected):raise ValueError('TQ_M1_GAP')
    return sorted(known.values(),key=lambda b:b.eob),added


def consume(worker):
    from .paper_runner import warmup_coverage
    c=worker.c;root=Path(c['root']);cs=c['csymbol'];r=load_manifest(c,root)['products'][cs]
    symbol=r['symbol'];worker.symbol=symbol
    metadata=read_json(root/r['metadata']);calendar=PaperCalendar(metadata['dates'],metadata['sessions'],cs);worker.calendar=calendar
    pin=Path(c['state_dir'])/'contract_pin.json'
    if pin.exists():
        saved=read_json(pin)
        if saved!={'symbol':symbol,'config_sha256':c['config_sha256']}:raise ValueError('TQ_PIN_CHANGED')
    else:atomic(pin,dict(symbol=symbol,config_sha256=c['config_sha256']))
    worker.events.put(('METADATA',metadata,calendar))
    seed=worker.bars(pd.read_parquet(root/r['seed']),pd.Timestamp.now(tz='Asia/Shanghai'))
    checkpoint=Path(c['state_dir'])/'core_checkpoint.json'
    if checkpoint.exists():
        saved=read_json(checkpoint);raw=saved.get('seed_cache') or {};cache=Path(raw.get('path','')).resolve()
        if raw.get('symbol')!=symbol or not cache.is_relative_to(root.resolve()/'data/cache/paper_market') or sha(cache)!=raw.get('sha256'):
            raise ValueError('TQ_RESTART_SEED_IDENTITY')
        seed=worker.bars(pd.read_parquet(cache),raw['cutoff'])
        from .paper_data import restore_tq_inputs
        seed=restore_tq_inputs(c,seed,saved.get('last_eob'),saved.get('tq_input_prefix'))
    spool=Path(c['tq_spool']);stem=cs.replace('.','_');started=time.monotonic();initialized=False;rollover_reported=False
    quote_tail=JsonlTail();bar_tail=JsonlTail()
    while not worker.stop:
        qpath=spool/(stem+'.quotes.jsonl');bpath=spool/(stem+'.bars.jsonl')
        if not initialized and (not qpath.exists() or not bpath.exists()):
            if time.monotonic()-started>180:raise TimeoutError('TQ_INITIAL_DATA_TIMEOUT')
            time.sleep(.2);continue
        proof=latest_committed(spool/'health.jsonl');now=datetime.now(timezone.utc)
        if proof.get('status')=='STARTING' and time.monotonic()-started<=180:
            time.sleep(.2);continue
        validate_envelope(dict(proof,symbol=symbol),c,symbol,now)
        if proof.get('status')!='RUNNING':raise ValueError('TQ_FEED_NOT_RUNNING')
        from .paper_symbol_health import enabled as isolated
        if isolated(c):
            ready=proof.get('product_status',{}).get(cs,{}).get('ready') is True
            if getattr(worker,'source_ready',None)!=ready:
                worker.events.put(('SOURCE_STATUS',dict(ready=ready,proof=proof.get('product_status',{}).get(cs))));worker.source_ready=ready
            if not ready:time.sleep(.2);continue
        for quote in quote_tail.read(qpath):
            validate_envelope(quote,c,symbol,quote['updated_at'])
            if clock(quote['received_at'])>datetime.now(timezone.utc):raise ValueError('TQ_QUOTE_RECEIPT_FUTURE')
            q=quote['quote']
            if q.get('source')!=SOURCE or q.get('source_run_id')!=c['tq_run_id']:raise ValueError('TQ_QUOTE_SOURCE_IDENTITY')
            worker.events.put(('QUOTE',symbol,q,quote['received_at']))
            if quote['rollover'] and not rollover_reported:
                worker.events.put(('ROLLOVER_FREEZE',dict(previous=symbol,source='TQ_MAIN_MAPPING_CHANGED')));rollover_reported=True
        for payload in bar_tail.read(bpath):
            now=datetime.now(timezone.utc)
            # Bars can remain unchanged in a quiet/closed session; quote progress is separately fresh.
            validate_envelope(payload,c,symbol,payload['updated_at'])
            if clock(payload['known_at'])>now:raise ValueError('TQ_BAR_KNOWN_AT_FUTURE')
            incoming=tq_rows(payload,symbol);merged,added=merge_same_source(seed,incoming,calendar)
            if any(b.eob>clock(payload['known_at']) for b in incoming):raise ValueError('TQ_FUTURE_CLOSED_BAR')
            if not initialized:
                coverage=warmup_coverage(merged,calendar,1200)
                cache=root/'data/cache/paper_market'/(stem+'_TQ_'+c['tq_run_id']);cache.mkdir(exist_ok=False,parents=True)
                frame=pd.DataFrame([dict(symbol=b.symbol,frequency=b.frequency,bob=b.bob,eob=b.eob,open=b.open,high=b.high,low=b.low,close=b.close,volume=b.volume,amount=b.amount,position=b.position) for b in merged])
                frame.to_parquet(cache/'initial_m1.parquet',index=False)
                atomic(cache/'input_manifest.json',dict(symbol=symbol,rows=len(frame),sha256=sha(cache/'initial_m1.parquet'),
                    source='TQ_DOWNLOADER_AND_TQ_LIVE_SNAPSHOT',known_at=payload['known_at'],historical_known_at='UNKNOWN',zero_trade_normalization=[]))
                worker.events.put(('INITIAL_CACHE',dict(path=str(cache/'initial_m1.parquet'),sha256=sha(cache/'initial_m1.parquet'),symbol=symbol,cutoff=now.isoformat()),coverage))
                initialized=True
            elif added:
                worker.events.put(('NEW_BARS',added,dict(requested_at='UNKNOWN_STREAM',returned_at=payload['known_at'],
                    enqueued_at=datetime.now(timezone.utc).isoformat(),source='TQ_CLOSED_M1',source_run_id=c['tq_run_id'])))
            seed=merged
        time.sleep(.2)
