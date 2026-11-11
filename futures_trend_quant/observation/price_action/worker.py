"""Bounded observer worker. Tokenless subprocess, no controller, no trading import."""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import multiprocessing
import os
import queue
from .models import POLICY, DEFAULTS, clock, digest, parameters
from .state import State, atomic


def _run(inbox, root, run_id, config, max_jobs=None):
    os.environ.pop('GM_TOKEN',None);os.environ.pop('GM_STRATEGY_ID',None)
    from .pipeline import Pipeline
    from futures_trend_quant.live.paper_io import read_json
    from futures_trend_quant.data.gm_bars import bars_from_records
    from futures_trend_quant.live.paper_calendar import PaperCalendar
    from backtest.aggregation import combine_bars
    import pandas as pd
    root=Path(root);ev=root/'evidence/paper_observation_five/price_action_shadow'
    state=State(root/'state/paper_observation_five/price_action_shadow',digest(config))
    pipeline=Pipeline(state,config);products={};sequence=0
    health=ev/'health.local.json'
    def publish(status,**more):
        atomic(health,dict(at=datetime.now(timezone.utc).isoformat(),pid=os.getpid(),run_id=run_id,
                          policy=POLICY,parameters_sha256=digest(config),status=status,sequence=sequence,
                          trade_api_called=False,token_present='GM_TOKEN' in os.environ,**more))
    def flush():
        from .cards import card
        for event_id,result in list(state.data.get('pending_results',{}).items()):
            path=ev/'snapshots'/str(result['trading_day'])/(event_id+'.local.json')
            if path.exists() and digest(read_json(path))!=digest(result):raise ValueError('PA_OUTPUT_IDENTITY')
            if not path.exists():atomic(path,result)
            if result.get('candidate_evidence_id'):atomic(ev/'cards'/(event_id+'.local.json'),card(result))
            atomic(ev/(result['csymbol'].replace('.','_')+'.local.json'),result)
            del state.data['pending_results'][event_id]
            state.save()
    flush()
    publish('STARTING')
    while True:
        try:
            item=inbox.get(timeout=10)
        except queue.Empty:
            publish('DEGRADED' if state.data['gaps'] else 'IDLE',gap_count=len(state.data['gaps']));continue
        try:
            kind,payload=item
            cs=payload['csymbol'];publish('PROCESSING',stage=kind,csymbol=cs)
            if kind=='METADATA':
                meta=payload['metadata'];cal=PaperCalendar(meta['dates'],meta['sessions'],cs)
                expected={}
                for t,d in zip(cal.clock,cal.days):expected.setdefault(str(d),set()).add(t.isoformat())
                products[cs]=dict(meta=meta,calendar=cal,bars={},received={},prefixes=[],last_snapshot=None,expected=expected)
            else:
                p=products[cs];known=payload['known_at']
                if kind=='CACHE':
                    path=Path(payload['path']).resolve()
                    if not path.is_relative_to((root/'data/cache/paper_market').resolve()):raise ValueError('PA_FOREIGN_CACHE')
                    if hashlib.sha256(path.read_bytes()).hexdigest()!=payload['sha256']:raise ValueError('PA_CACHE_HASH')
                    bars=bars_from_records(pd.read_parquet(path).to_dict('records'))
                    p['prefixes'].append(payload['sha256'])
                elif kind=='BARS':
                    bars=payload['bars']
                    p['prefixes'].append(digest([asdict(b) for b in bars]))
                elif kind=='CANDIDATE':bars=[]
                else:raise ValueError('PA_UNKNOWN_JOB')
                for b in bars:
                    if b.symbol!=p['meta']['symbol']:raise ValueError('PA_CONTRACT_IDENTITY')
                    old=p['bars'].get(b.identity)
                    if old is not None and old!=b:raise ValueError('PA_INPUT_REVISION')
                    p['bars'].setdefault(b.identity,b);p['received'].setdefault(b.identity,known)
                ordered=sorted(p['bars'].values(),key=lambda b:b.eob)[-24000:]
                p['bars']={b.identity:b for b in ordered}
                p['received']={k:v for k,v in p['received'].items() if k in p['bars']}
                m15,m5=complete_bars(ordered,15),complete_bars(ordered,5)
                if kind=='CANDIDATE':
                    cutoff=clock(payload['candidate']['bar_eob'])
                    m15=[b for b in m15 if b.eob<=cutoff];m5=[b for b in m5 if b.eob<=cutoff]
                latest_key=None if not m15 else (m15[-1].identity,None if not m5 else m5[-1].identity)
                if m15 and (kind=='CANDIDATE' or p['last_snapshot']!=latest_key):
                    received=aggregate_receipts(m15+m5,ordered,p['received'])
                    cal=p['calendar'];day_by_id={b.identity:cal.day(b.bob) for b in m15}
                    # Complete previous days require exact expected M1 coverage.
                    expected=p['expected']
                    seen={}
                    for b in ordered:seen.setdefault(cal.day(b.bob),set()).add(b.eob.isoformat())
                    complete=[d for d,times in expected.items() if times and times<=seen.get(d,set())]
                    present={b.eob.isoformat() for b in ordered}
                    required={t.isoformat() for t in cal.clock if ordered[0].eob<=t<=ordered[-1].eob}
                    from .coverage import snapshot_coverage
                    input_quality=snapshot_coverage(ordered,cal,p['received'],m15[-1200:],m5[-1200:])
                    coverage=input_quality['status']
                    asof=datetime.now(timezone.utc).isoformat()
                    result=pipeline.snapshot(symbol=p['meta']['symbol'],csymbol=cs,m15=m15[-1200:],m5=m5[-1200:],
                        received=received,tick=float(p['meta']['spec']['price_tick']),asof=asof,
                        trading_day=cal.day(m15[-1].bob),session_id=cal.section_for_bar(m15[-1]),
                        source_prefix_sha256=digest(p['prefixes']),candidate=payload.get('candidate'),
                        complete_days=complete,day_by_id=day_by_id,completeness=coverage,input_quality=input_quality)
                    if result is not None:
                        from futures_trend_quant.live.paper_watermark import closure_status
                        result['source_closure']=closure_status(cal,dict(last_closed_m1=ordered[-1].eob.isoformat(),last_closed_m15=m15[-1].eob.isoformat()),asof,'CURRENT_RUN_OBSERVER_INPUT')
                        state.data['pending_results'][result['event_id']]=result
                        state.save()
                    flush()
                    if kind!='CANDIDATE':p['last_snapshot']=latest_key
            sequence+=1;publish('DEGRADED' if state.data['gaps'] else 'IDLE',gap_count=len(state.data['gaps']))
            if max_jobs is not None and sequence>=max_jobs:return
        except Exception as exc:
            state.data['gaps'].append(dict(at=datetime.now(timezone.utc).isoformat(),category=type(exc).__name__,
                                          reason=str(exc)[:100],stage=item[0]))
            state.save();publish('DEGRADED',diagnostic=dict(category=type(exc).__name__,stage=item[0]))


class Observer:
    def __init__(self,root,run_id):
        self.root=Path(root);self.run_id=run_id;self.dropped=0;self.failed=None;self.process=None
        from futures_trend_quant.live.paper_io import read_json
        path=self.root/'config/price_action_shadow.local.json'
        try:self.config=parameters(read_json(path))
        except Exception as exc:
            self.failed=type(exc).__name__;return
        ctx=multiprocessing.get_context('spawn');self.inbox=ctx.Queue(maxsize=self.config['queue_size'])
        # Child strips token before imports; no account/config credentials are sent.
        self.process=ctx.Process(target=_run,args=(self.inbox,str(root),run_id,self.config),daemon=True,name='paper-price-action-shadow')
        self.process.start()

    def offer(self,kind,**payload):
        if not self.process or not self.process.is_alive():self.dropped+=1;return False
        try:self.inbox.put_nowait((kind,payload));return True
        except queue.Full:self.dropped+=1;return False
        except Exception as exc:self.failed=type(exc).__name__;self.dropped+=1;return False

    def health(self):
        result=dict(mode='SHADOW',policy=POLICY,parameters_sha256=digest(getattr(self,'config',DEFAULTS)),
                    process_alive=bool(self.process and self.process.is_alive()),queue_gaps=self.dropped,
                    diagnostic=self.failed,affects_execution=False)
        try:
            from futures_trend_quant.live.paper_io import read_json
            row=read_json(self.root/'evidence/paper_observation_five/price_action_shadow/health.local.json')
            age=(datetime.now(timezone.utc)-clock(row['at'])).total_seconds()
            result['worker']=row;result['fresh']=0<=age<=30 and row['run_id']==self.run_id
        except Exception:result['fresh']=False
        result['status']='AVAILABLE' if result['process_alive'] and result['fresh'] and not self.dropped and not self.failed and result.get('worker',{}).get('status') not in ('DEGRADED',) else 'UNKNOWN_WITH_GAP'
        return result


def complete_bars(bars,period):
    from backtest.aggregation import combine_bars
    groups={};width=period*60
    for b in bars:
        end=((int(b.eob.timestamp())+width-1)//width)*width
        groups.setdefault((b.symbol,end),[]).append(b)
    result=[]
    for (_,end),members in sorted(groups.items(),key=lambda pair:pair[0][1]):
        members.sort(key=lambda b:b.eob)
        if len(members)==period and all(b.eob.timestamp()==end-(period-1-i)*60 for i,b in enumerate(members)):
            result.append(combine_bars(members,str(width)+'s'))
    return result


def aggregate_receipts(aggregates,minutes,received):
    """Linear in the aggregate input size; never rescan a 24,000-bar seed per bar."""
    from datetime import timedelta
    lookup={b.eob.timestamp():received.get(b.identity) for b in minutes};result={}
    for agg in aggregates:
        n=int((agg.eob-agg.bob).total_seconds()/60)
        times=[lookup.get((agg.bob+timedelta(minutes=i+1)).timestamp()) for i in range(n)]
        result[agg.identity]=max(times,key=clock) if times and all(times) else None
    return result
