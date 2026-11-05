"""S0_SHORT_PAPER_V1 coordinator; deadline loop independent of data/RPC workers."""
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import queue
import time
import pandas as pd
import numpy as np
from futures_trend_quant.core.facts.indicators import calculate_indicators
from .paper_config import ROOT,load_paper_config
from .paper_broker import PaperEngine,atomic,TERMINAL,stamp
from .paper_data import MarketWorker,SignalWorker,complete_bars
from .paper_transport import SimTransport


def warmup_coverage(bars,calendar,required=1200):
    actual={pd.Timestamp(b.eob) for b in bars}
    if not actual:return dict(qualified=False,reason='M1_EMPTY')
    cutoff=max(actual).floor('15min')
    expected=calendar.clock[calendar.clock<=cutoff]
    bucket_ns=expected.ceil('15min').as_unit('ns').asi8
    ends,counts=np.unique(bucket_ns,return_counts=True);selected=ends[counts==15][-required:]
    selected_minutes=expected[np.isin(bucket_ns,selected)]
    actual_ns=np.array([x.value for x in actual],dtype=np.int64)
    missing=int((~np.isin(selected_minutes.as_unit('ns').asi8,actual_ns)).sum())
    return dict(qualified=len(selected)==required and missing==0,expected_m15=len(selected),missing_m1=missing,
        first_expected=None if not len(selected_minutes) else selected_minutes[0].isoformat(),last_expected=None if not len(selected_minutes) else selected_minutes[-1].isoformat())


class PaperRunner:
    def __init__(self,config_path,readonly=False):
        self.config_path=Path(config_path).resolve();self.c=load_paper_config(config_path);self.readonly=readonly
        if readonly:
            suffix='preflight_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
            self.c['state_dir']=str(ROOT/'data/cache/paper_readonly'/suffix/'state')
            self.c['evidence_dir']=str(ROOT/'data/cache/paper_readonly'/suffix/'evidence')
        self.evidence=Path(self.c['evidence_dir']);self.evidence.mkdir(parents=True,exist_ok=True)
        self._acquire_lock()
        self.transport=SimTransport(self.c,self.config_path)
        self.market=MarketWorker(self.c);self.signals=SignalWorker(self.c)
        self.engine=None;self.metadata=None;self.calendar=None;self.last_m15=None;self.test_signal=None
        self.coverage=dict(qualified=False,reason='NOT_YET_CHECKED');self.data_frozen=False;self.core_ready=False
        self.last_report=0.;self.last_snapshot_request=0.;self.last_test_attempt=0.;self.fill_notified=set()
        self.last_fault=None
        self.started=pd.Timestamp.now(tz='Asia/Shanghai');self.last_loop=time.monotonic();self.max_loop_gap=0.
        self.run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        self.counts=Counter();self.minute_seen={};self.quote_seen={};self.full_days=[]
        file=Path(self.c['state_dir'])/'observation_minutes.json'
        if file.exists():
            state=json.loads(file.read_text(encoding='utf8'))
            self.minute_seen={k:set(v) for k,v in state.get('minutes',{}).items()};self.quote_seen={k:set(v) for k,v in state.get('quotes',{}).items()}
            self.counts.update(state.get('counts',{}))
        manifest=dict(strategy='S0_SHORT_PAPER_V1',started_at=self.started.isoformat(),readonly=readonly,
            paper_authorized=True,live_orders_enabled=False,account_hash=hashlib.sha256(self.c['account_id'].encode()).hexdigest()[:12],
            config_sha256=self.c['config_sha256'],base_rules_unchanged=True,execution_differences='ONE_LOT_NO_ADD; PAPER_RISK_LIMITS; DPLUS1_AND_LONG_CLOSURE_DISCIPLINE',
            mode='OFFICIAL_GM_SIM',market_source='GM_CURRENT_SNAPSHOT_1S_AND_CLOSED_M1_POLL_5S; NOT_TICK_STREAM',
            active_subscriptions=[],subscription_count=0,order_callback_mode='ACCOUNT_SCOPED_POLL_1S_WHEN_OPEN_OR_EXPOSED',
            five_clocks='MARKET_EVENT; LOCAL_API_RESPONSE; CORE_READY; INTENT_QUEUED/SENT; ORDER_ACK/FILL_REPORT',
            real_latency_proven=False,fees_label='PLATFORM_SIMULATED_DEDUCTIONS_NOT_CERTIFIED_REAL_FEES',
            first_partial_day_not_full_observation_day=True,independent_score_run=False,
            formal90_original_whole_pool_window='NO_LONGER_UNSEEN_FROM_AUTHORIZED_PAPER_DATA_ACCESS',
            checkpoints=[5,20],review_mode='SINGLE_AGENT_SELF_CHECK',source_hashes={})
        for p in list((ROOT/'futures_trend_quant/live').glob('paper_*.py'))+[ROOT/'config/strategy.json',ROOT/'config/paper_observation_usage.json']:
            manifest['source_hashes'][str(p.relative_to(ROOT)).replace('\\','/')]=hashlib.sha256(p.read_bytes()).hexdigest()
        manifest['run_id']=self.run_id
        atomic(self.evidence/('run_manifest_'+self.run_id+'.json'),manifest)
        atomic(self.evidence/'run_manifest.json',manifest)
        self.market.thread.start();self.signals.thread.start()

    def _acquire_lock(self):
        import msvcrt
        if not self.readonly:
            import ctypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CreateMutexW.restype=ctypes.c_void_p
            kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]
            ctypes.set_last_error(0)
            self.account_mutex=kernel.CreateMutexW(None,False,'Local\\FTQ_GM_PAPER_'+hashlib.sha256(self.c['account_id'].encode()).hexdigest()[:24])
            if not self.account_mutex or ctypes.get_last_error()==183:raise RuntimeError('another paper execution controller already owns this account')
        path=Path(self.c['state_dir'])/'runner.lock';path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=path.open('a+b')
        self.lock.seek(0)
        if path.stat().st_size==0:self.lock.write(b'0');self.lock.flush();self.lock.seek(0)
        msvcrt.locking(self.lock.fileno(),msvcrt.LK_NBLCK,1)

    def event(self,kind,**fields):
        with (self.evidence/'runtime_events.jsonl').open('a',encoding='utf8') as f:
            f.write(json.dumps(dict(at=pd.Timestamp.now(tz='Asia/Shanghai').isoformat(),kind=kind,**fields),ensure_ascii=False,default=str)+'\n')

    def _market_events(self,now):
        while True:
            try:event=self.market.events.get_nowait()
            except queue.Empty:return
            kind=event[0]
            if kind=='METADATA':
                self.metadata=event[1];self.calendar=event[2]
                self.c['fixed_actual_symbol']=self.metadata['symbol']
                if self.engine is None:self.engine=PaperEngine(self.c,self.calendar,self.transport)
            elif kind=='INITIAL_BARS':
                self.coverage=warmup_coverage(event[1],self.calendar,self.c['warmup_m15'])
                self.signals.inbox.put(('INITIAL',event[1]))
                self.event('WARMUP_COVERAGE',**self.coverage)
            elif kind=='QUOTE' and self.engine is not None:
                _,symbol,q,received=event
                self.engine.quote(symbol,q,received)
                valid,_=self.engine.valid_quote(symbol,received)
                if valid is not None and self.calendar.market_open(received):
                    day=self.calendar.day(received);eob=stamp(received).floor('min')+pd.Timedelta(minutes=1)
                    self.quote_seen.setdefault(day,set()).add(eob.isoformat())
                with (self.evidence/'quotes.jsonl').open('a',encoding='utf8') as f:
                    f.write(json.dumps(dict(symbol=symbol,local_received_at=received,quote=q),default=str)+'\n')
            elif kind=='NEW_BARS':
                for bar in event[1]:
                    if self.engine:self.engine.m1(bar,now)
                    if 0<=(now-stamp(bar.eob)).total_seconds()<=10:
                        self.minute_seen.setdefault(self.calendar.day(bar.bob),set()).add(pd.Timestamp(bar.eob).isoformat())
                    else:self.counts['M1_BACKFILL_OR_LATE']+=1
                    with (self.evidence/'m1_observed.jsonl').open('a',encoding='utf8') as f:
                        f.write(json.dumps(dict(symbol=bar.symbol,bob=str(bar.bob),eob=str(bar.eob),open=bar.open,high=bar.high,
                            low=bar.low,close=bar.close,volume=bar.volume,local_observed_at=str(now),source='GM_CLOSED_M1_POLL'))+'\n')
                self.signals.inbox.put(('NEW',event[1]))
            elif kind in ('DATA_FAILED','DATA_GAP','ROLLOVER_FREEZE'):
                self.data_frozen=True;self.event(kind,detail=event[1])

    def _core_events(self,now):
        while True:
            try:event=self.signals.outbox.get_nowait()
            except queue.Empty:return
            if event[0]=='CORE_READY':
                self.core_ready=True;self.last_m15=event[2]
                atr=event[1].get('atr14')
                if atr is not None and self.metadata:
                    spec=self.metadata['spec']
                    self.test_signal=dict(symbol=self.metadata['symbol'],direction='bull',evidence_id='',signal_at=str(now),
                        anchor=float(self.last_m15.low),atr=float(atr),multiplier=float(spec['multiplier']),margin_ratio=float(spec['margin_ratio']),
                        rules=self.c['base_config'].raw['rules'],path='CONNECTIVITY_TEST',m15_index=self.signals.runtime.m15_index_for('DCE.M'))
                self.event('CORE_READY',progress=event[1])
            elif event[0] in ('WARMUP_FAILED','CORE_FAILED'):
                self.core_ready=False;self.data_frozen=True;self.event(event[0],detail=event[1])
            elif event[0]=='CORE_RESULT' and self.engine is not None:
                result,bar=event[1],event[2];self.last_m15=bar;self.counts['M15_DECISIONS']+=1
                now=pd.Timestamp.now(tz='Asia/Shanghai')
                ready_at=event[3] if len(event)>3 else now.isoformat()
                if not self.readonly and result.trend.confirmed:self.engine.force_exit(bar.symbol,'TREND_EXIT',now,direction=result.trend.confirmed_state)
                entry=result.entry;decision=None
                if entry is not None and entry.accepted:
                    self.counts['TECHNICAL_SIGNALS']+=1
                    if not self.readonly and not self.data_frozen and self.coverage.get('qualified') and self.core_ready and result.anchor is not None:
                        spec=self.metadata['spec'];atr=result.analysis.indicators.atr14[-1]
                        if atr is not None:
                            s=dict(symbol=bar.symbol,direction=entry.direction,evidence_id=entry.evidence_id,signal_at=bar.eob.isoformat(),
                                anchor=result.anchor.anchor_price,atr=float(atr),multiplier=float(spec['multiplier']),margin_ratio=float(spec['margin_ratio']),
                                rules=self.c['base_config'].raw['rules'],path=entry.path,m15_index=result.m15_index)
                            decision=self.engine.open(s,now)
                            self.counts['ORDER_ALLOWED' if decision[0] else 'BLOCKED_'+decision[1]]+=1
                    else:decision=(False,'DATA_PREHEAT_OR_READONLY')
                with (self.evidence/'signals.jsonl').open('a',encoding='utf8') as f:
                    f.write(json.dumps(dict(bar_eob=bar.eob.isoformat(),signal_ready_at=ready_at,order_evaluated_at=str(now),facts=result.facts_row,
                        entry=None if entry is None else entry.__dict__,paper_decision=decision),ensure_ascii=False,default=str)+'\n')

    def _connectivity(self,now):
        e=self.engine
        if e is None or e.s['connectivity_passed'] or not e.ready or self.data_frozen or not self.core_ready or not self.coverage.get('qualified'):return
        if self.calendar.entry_block(now) or self.test_signal is None:return
        test=[i for i in e.s['intents'].values() if i['tag']=='CONNECTIVITY_TEST']
        positive=[p for p in e.positions() if int(p['volume'])>0]
        # Any test fill immediately enters the same supervised exit path.
        for p in positive:
            key=e.key(p['symbol'],'bull' if int(p['side'])==1 else 'bear')
            meta=e.s['meta'].get(key)
            if meta and meta['tag']=='CONNECTIVITY_TEST' and not meta.get('force_exit'):meta['force_exit']='CONNECTIVITY_TEST_CLOSE';e.save()
        if positive or e.active():return
        cancel_ok=any(i['effect']==1 and i.get('test_cancel') and i['status']==5 and not i['filled'] for i in test)
        roundtrip_ok=any(i['effect']!=1 and i.get('filled')==1 and i.get('closed_confirmed') for i in test)
        if cancel_ok and roundtrip_ok:
            e.s.update(connectivity_stage='PASSED',connectivity_passed=True);e.save();e.event('CONNECTIVITY_TEST_PASSED_FLAT_CONFIRMED')
            return
        attempts=[i for i in test if i['effect']==1]
        if any(i['status'] in (8,12) for i in attempts) or len(attempts)>=3:
            e.s['connectivity_stage']='BLOCKED_REVIEW_REQUIRED';e.s['halt_reason']='CONNECTIVITY_REJECTED_OR_ATTEMPT_LIMIT';e.save();return
        if time.monotonic()-self.last_test_attempt<10:return
        signal=dict(self.test_signal,signal_at=str(now),evidence_id='CONNECTIVITY_TEST:'+str(len(attempts)+1))
        ok,reason=e.open(signal,now,tag='CONNECTIVITY_TEST',test_cancel=not cancel_ok)
        self.last_test_attempt=time.monotonic()
        if ok:
            e.s['connectivity_stage']='WAIT_CANCEL' if not cancel_ok else 'WAIT_ROUNDTRIP';e.save()
        else:self.event('CONNECTIVITY_WAIT',reason=reason)

    def report(self,now):
        summary=None if self.engine is None else self.engine.summary()
        expected={d:int((self.calendar.days==d).sum()) for d in set(self.calendar.days)} if self.calendar else {}
        counts={d:len(s & self.quote_seen.get(d,set())) for d,s in self.minute_seen.items()}
        full=sorted(d for d,n in counts.items() if n==expected.get(d,-1) and now>=pd.Timestamp(d+'T15:01:00+08:00'))
        self.full_days=full
        if self.engine is not None and len(full)>=20 and not self.readonly:
            self.engine.s['pause_new']=True;self.engine.save()
        phase='INITIALIZING'
        if summary:
            if self.readonly:phase='READONLY_PREFLIGHT_PASS' if self.core_ready and self.coverage.get('qualified') and self.engine.ready and not self.data_frozen else 'READONLY_PREFLIGHT_PENDING'
            elif not self.engine.ready:phase='PENDING_RECON_MANAGING_POSITIONS'
            elif self.data_frozen:phase='DATA_FROZEN_MANAGING_POSITIONS'
            elif self.engine.s['halt_reason'] or self.engine.s['pause_new']:phase='STOP_NEW_OPEN_MANAGING_POSITIONS'
            elif self.engine.s['connectivity_passed']:phase='NATURAL_SIGNAL_OBSERVATION'
            elif self.core_ready and self.coverage.get('qualified'):phase='CONNECTIVITY_WAIT_SESSION_OR_QUOTE'
        closed=[]
        if self.engine is not None:
            for i in self.engine.s['intents'].values():
                if i['effect']==1 or not i.get('closed_confirmed') or not i.get('entry_price') or not i.get('filled_vwap'):continue
                risk=abs(i['entry_price']-i['initial_stop']);sign=1 if i['direction']=='bull' else -1
                gross=sign*(i['filled_vwap']-i['entry_price'])*i['multiplier']
                closed.append(dict(intent_id=i['id'],tag=i['tag'],symbol=i['symbol'],reason=i['reason'],
                    entry=i['entry_price'],exit=i['filled_vwap'],gross_pnl=gross,gross_r=None if risk<=0 else gross/(risk*i['multiplier']),
                    platform_per_trade_fee='SEE_EXECUTION_REPORT_COMMISSION; NOT_CERTIFIED_REAL_COST'))
            atomic(self.evidence/'closed_trades.local.json',dict(trades=closed))
        status=dict(at=str(now),phase=phase,readonly=self.readonly,symbol=None if self.metadata is None else self.metadata['symbol'],
            warmup=self.coverage,core=self.signals.progress,broker=summary,counts=dict(self.counts),
            complete_observation_days=full,qualified_minute_buckets_by_day=counts,expected_minutes_by_day={d:expected.get(d) for d in counts},
            five_day_execution_review_due=len(full)>=5,twenty_day_review_due=len(full)>=20,
            maximum_main_loop_gap_seconds=self.max_loop_gap,source_polling_not_tick_completeness=True,
            shadow_account_comparison_completed=False,independent_profitability_proven=False)
        status['closed_trade_counts']={tag:sum(x['tag']==tag for x in closed) for tag in ('STRATEGY','CONNECTIVITY_TEST')}
        status['simulated_fill_gross_pnl']={tag:sum(x['gross_pnl'] for x in closed if x['tag']==tag) for tag in ('STRATEGY','CONNECTIVITY_TEST')}
        status['worker_health']=dict(market_alive=self.market.thread.is_alive(),core_alive=self.signals.thread.is_alive(),
            broker_alive=self.transport.process.poll() is None,broker_hung_or_disconnected=self.transport.hung(),
            snapshot_age_seconds=None if self.engine is None or self.engine.last_snapshot is None else (now-stamp(self.engine.last_snapshot)).total_seconds())
        atomic(self.evidence/'status.json',status)
        atomic(Path(self.c['state_dir'])/'observation_minutes.json',dict(minutes={k:sorted(v) for k,v in self.minute_seen.items()},quotes={k:sorted(v) for k,v in self.quote_seen.items()},counts=dict(self.counts)))
        text=f"# S0_SHORT_PAPER_V1 本地模拟观察\n\n时间：{now}\n\n阶段：{phase}\n\n实际合约：{status['symbol']}；完整观察交易日：{len(full)}/20。\n\n"
        if summary:
            text+=f"GM 模拟净值：{summary['nav']}；含浮盈亏权益变化：{summary['platform_equity_change']}；平台模拟扣费：{summary['platform_simulated_deductions']}。费用未核验，不称真实净收益。\n\n"
            text+=f"对账：{summary['status']}；待处理意图：{summary['active_intents']}；联调通过：{summary['connectivity_passed']}；停开原因：{summary['halt_reason']}。\n\n"
            text+='持仓：\n\n'+json.dumps(summary['positions'],ensure_ascii=False,indent=2)+'\n\n止损/目标及截止：\n\n'+json.dumps(summary['position_plans'],ensure_ascii=False,indent=2)+'\n\n应平事件：\n\n'+json.dumps(summary['due_events'],ensure_ascii=False,indent=2)+'\n'
        text+='\n联调交易单列。无信号或未成交如实记录；没有完成简单入场影子账户对照。此为运行快照，完整流水见同目录 JSONL/CSV。\n'
        (self.evidence/'daily_report.md').write_text(text,encoding='utf8')
        # Retain one final daily view in addition to append-only raw evidence.
        (self.evidence/('daily_report_'+self.calendar.day(now)+'.md') if self.calendar else self.evidence/'daily_report_initializing.md').write_text(text,encoding='utf8')
        return status

    def run(self,max_seconds=0):
        started=time.monotonic()
        while True:
            now=pd.Timestamp.now(tz='Asia/Shanghai');mono=time.monotonic()
            self.max_loop_gap=max(self.max_loop_gap,mono-self.last_loop);self.last_loop=mono
            try:self._market_events(now);self._core_events(now)
            except Exception as exc:
                self.data_frozen=True
                fault='DATA_HANDOFF_'+type(exc).__name__+':'+str(exc)[:160]
                if self.last_fault!=fault:self.event('INTERNAL_DATA_FAULT',detail=fault);self.last_fault=fault
            for job,reply in self.transport.drain():
                if self.engine is not None:self.engine.accept(job,reply,pd.Timestamp.now(tz='Asia/Shanghai'))
            if self.engine is not None:
                positions=Counter('bull' if int(p['side'])==1 else 'bear' for p in self.engine.positions() if int(p['volume'])>0)
                self.signals.inbox.put(('POSITIONS',dict(positions)))
                for i in self.engine.s['intents'].values():
                    if i['effect']==1 and i.get('filled') and i['tag']=='STRATEGY' and i['id'] not in self.fill_notified:
                        self.signals.inbox.put(('FILL',dict(i)));self.fill_notified.add(i['id'])
                control=Path(self.c['state_dir'])/'control.json'
                if control.exists():
                    try:
                        raw=json.loads(control.read_text(encoding='utf8'))
                        if raw.get('stop_new_open') is True:self.engine.s['pause_new']=True
                        if raw.get('flatten') is True:
                            for meta in self.engine.s['meta'].values():meta['force_exit']='USER_CONTROL_FLATTEN'
                    except ValueError:self.engine.s['pause_new']=True
                if not self.readonly:
                    try:self.engine.tick(now);self._connectivity(now)
                    except Exception as exc:
                        self.engine.ready=False;self.engine.block='INTERNAL_EXECUTION_FAULT';self.engine.s['pause_new']=True
                        fault=type(exc).__name__+':'+str(exc)[:160]
                        if self.last_fault!=fault:self.event('INTERNAL_EXECUTION_FAULT',detail=fault);self.last_fault=fault
                interval=1 if self.calendar.market_open(now) or self.engine.active() or any(int(p['volume'])>0 for p in self.engine.positions()) else 5
                if not self.transport.busy() and mono-self.last_snapshot_request>=interval:
                    self.transport.request('snapshot');self.last_snapshot_request=mono
            if mono-self.last_report>=5:
                try:status=self.report(now)
                except Exception as exc:
                    self.event('REPORT_FAILED_RISK_LOOP_CONTINUES',error=type(exc).__name__);status={}
                self.last_report=mono
                if self.readonly and status.get('phase')=='READONLY_PREFLIGHT_PASS':
                    print(json.dumps(dict(phase=status['phase'],symbol=status['symbol'],warmup=status['warmup'],status_file=str(self.evidence/'status.json')),ensure_ascii=False),flush=True)
                    return status
            if max_seconds and mono-started>=max_seconds:return self.report(now)
            time.sleep(.2)


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--preflight',action='store_true');p.add_argument('--max-seconds',type=int,default=0)
    a=p.parse_args();runner=PaperRunner(a.config,readonly=a.preflight)
    print(json.dumps(dict(started=True,readonly=a.preflight,pid=os.getpid(),evidence_dir=str(runner.evidence))),flush=True)
    runner.run(a.max_seconds)


if __name__=='__main__':main()
