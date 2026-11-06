"""Persistent one-lot simulation execution and reconciliation state machine.

No GM default account, no optimistic close completion, no resubmission of an
uncertain open. The caller invokes tick independently of quote delivery.
"""
from pathlib import Path
from datetime import datetime,timezone
import csv
import hashlib
import json
import math
import os
import tempfile
import time
import uuid
import pandas as pd
from .paper_products import product_of,close_effect
from .paper_anchor_guard import PROOF_FIELDS,EXECUTION_FIELDS,validate_anchor_proof,validate_execution_proof
from .paper_risk_controls import enabled as controls_enabled,cooldown_reason,update_roundtrips,observe_shadow

TERMINAL={3,5,8,12}


def stamp(x):
    t=pd.Timestamp(x)
    if pd.isna(t) or t.tzinfo is None:raise ValueError('unknown/naive timestamp')
    return t


def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name,suffix='.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(value,f,ensure_ascii=False,allow_nan=False,sort_keys=True,default=str);f.write('\n');f.flush();os.fsync(f.fileno())
        for attempt in range(5):
            try:os.replace(tmp,path);break
            except PermissionError as exc:
                if getattr(exc,'winerror',None) not in (5,32,33) or attempt==4:raise
                time.sleep(.01*(attempt+1))  # Bounded 100ms total; permanent failures still halt.
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


class PaperEngine:
    def __init__(self,config,calendar,transport):
        self.c=config;self.calendar=calendar;self.transport=transport;self.quotes={};self.snapshot=None
        self.last_snapshot=None;self.last_poll=None;self.ready=False;self.block='STARTUP_RECON'
        self.path=Path(config['state_dir'])/'execution.json';self.evidence=Path(config['evidence_dir']);self.evidence.mkdir(parents=True,exist_ok=True)
        if self.path.exists():
            self.s=json.loads(self.path.read_text(encoding='utf8'))
            if self.s['config_sha256']!=config['config_sha256'] or self.s['account_id']!=config['account_id']:raise ValueError('execution state account/config mismatch')
        else:
            self.s=dict(schema_version=1,config_sha256=config['config_sha256'],account_id=config['account_id'],
                intents={},used_signals=[],meta={},exec_ids=[],realized_gross=0.,day='',day_nav=None,peak_nav=None,
                initial_nav=None,initial_commission=None,last_nav=None,connectivity_stage='CANCEL',connectivity_passed=False,
                pause_new=False,halt_reason='',due_events={},snapshot_count=0)
        self.save()
        self.pending = {}  # Never replay unsubmitted candidates across a restart.
        self.s.setdefault('execution_fees', {})
        for key, row in self.s.setdefault('candidate_status', {}).items():
            if row.get('status') == 'WAITING':
                row.update(status='ABORTED_RESTART', reason='NO_HISTORICAL_RESEND')
        self.save()

    def save(self):atomic(self.path,self.s)

    def event(self,kind,**fields):
        # Full details are private; only aggregate deployment receipts are published.
        with (self.evidence/'order_events.jsonl').open('a',encoding='utf8') as f:
            f.write(json.dumps(dict(recorded_at=datetime.now(timezone.utc).isoformat(),kind=kind,**fields),ensure_ascii=False,default=str)+'\n');f.flush()

    def key(self,symbol,direction):return symbol+'|'+direction
    def cal(self,symbol):return self.calendar.for_symbol(symbol) if hasattr(self.calendar,'for_symbol') else self.calendar

    def quote(self,symbol,quote,now):
        self.quotes[symbol]=dict(quote,received_at=str(now))
        for evidence,signal in list(self.pending.items()):
            if signal['symbol']!=symbol:continue
            try:
                created=stamp(quote['created_at']);last=float(quote['last'])
                if not stamp(signal['signal_at'])<=created<=stamp(now)+pd.Timedelta(seconds=1):continue
                broken=last<float(signal['anchor']) if signal['direction']=='bull' else last>float(signal['anchor'])
                if math.isfinite(last) and last>0 and broken:
                    self._finish_candidate(evidence,'INVALIDATED','ANCHOR_BROKEN_WHILE_WAITING',now)
            except (ValueError,TypeError,KeyError):continue

    def valid_quote(self,symbol,now):
        q=self.quotes.get(symbol)
        if not q:return None,'QUOTE_MISSING'
        try:
            age=(stamp(now)-stamp(q['created_at'])).total_seconds()
            if not -1<=age<=self.c['quote_max_age_seconds']:return None,'QUOTE_STALE_OR_FUTURE'
            received_age=(stamp(now)-stamp(q['received_at'])).total_seconds()
            if not 0<=received_age<=3:return None,'RECEPTION_STALE'
            bid=float(q['bid']);ask=float(q['ask']);tick=float(q['tick'])
            if not all(math.isfinite(x) and x>0 for x in (bid,ask,tick)) or bid>ask:return None,'BOOK_UNAVAILABLE'
            sizes=[float(q['bid_size']),float(q['ask_size'])]
            if not all(math.isfinite(x) and x>=1 for x in sizes):return None,'NO_OPPONENT_SIZE'
            if not math.isfinite(float(q.get('last',bid))) or float(q.get('last',bid))<=0:return None,'LAST_PRICE_UNKNOWN'
            if (ask-bid)/tick>self.c['max_spread_ticks']+1e-8:return None,'SPREAD_LIMIT'
        except (ValueError,KeyError,TypeError):return None,'QUOTE_FIELDS_UNKNOWN'
        return q,'OK'

    def positions(self):return [] if self.snapshot is None else self.snapshot['positions']

    def active(self,symbol=None,effect=None):
        return [x for x in self.s['intents'].values() if x.get('status') not in TERMINAL and
                (symbol is None or x['symbol']==symbol) and (effect is None or (x['effect']!=1 if effect==2 else x['effect']==effect))]

    def _request(self,op,args,rid):
        self.transport.request(op,args,rid=rid)

    def _send(self,intent,now):
        rid=intent['id'];intent.update(submitted_at=str(now),status=10,filled=0,filled_vwap=0.,order_id='',cancel_requested=False,
            decision_quote=dict(self.quotes[intent['symbol']]))
        self.s['intents'][rid]=intent
        self.save()  # Mandatory write-ahead record precedes the RPC queue.
        self.event('ORDER_INTENT',intent=intent)
        q=self.quotes[intent['symbol']]
        self._request('submit',dict(symbol=intent['symbol'],csymbol=intent['csymbol'],volume=1,side=intent['side'],
            position_effect=intent['effect'],price=intent['price'],stop=intent.get('stop'),
            **{k:intent.get(k) for k in PROOF_FIELDS},
            **{k:intent.get(k) for k in EXECUTION_FIELDS},
            multiplier=intent['multiplier'],margin_ratio=intent['margin_ratio'],
            quote_event_at=q['created_at'],quote_received_at=q['received_at'],
            quote_bid=q['bid'],quote_ask=q['ask'],tick=q['tick'],day_nav=self.s['day_nav'],peak_nav=self.s['peak_nav']),rid)
        return rid

    def open(self,signal,now,tag='STRATEGY',test_cancel=False):
        reason=None
        symbol=signal['symbol'];evidence=signal['evidence_id']
        csymbol=product_of(symbol)
        if csymbol not in self.c.get('universe',['DCE.M']):return False,'PRODUCT_NOT_AUTHORIZED'
        if evidence in self.s['used_signals']:reason='DUPLICATE_SIGNAL'
        elif not self.ready or self.last_snapshot is None or (stamp(now)-stamp(self.last_snapshot)).total_seconds()>3:reason='PENDING_RECON'
        elif self.s['pause_new'] or self.s['halt_reason']:reason=self.s['halt_reason'] or 'STOP_NEW_OPEN'
        elif controls_enabled(self.c) and cooldown_reason(self.s,now,self.calendar):reason=cooldown_reason(self.s,now,self.calendar)
        elif tag=='STRATEGY' and not self.s['connectivity_passed']:reason='CONNECTIVITY_NOT_PASSED'
        elif self.transport.busy():reason='BROKER_BUSY'
        elif self.cal(symbol).entry_block(now):reason=self.cal(symbol).entry_block(now)
        elif not 0<=(stamp(now)-stamp(signal['signal_at'])).total_seconds()<=self.c['signal_max_age_seconds']:reason='SIGNAL_EXPIRED_OR_FUTURE'
        elif any(int(p['volume'])>0 for p in self.positions() if p['symbol']==symbol) or self.active(symbol,effect=1):reason='PRODUCT_POSITION_OR_OPEN_INTENT'
        elif sum(int(p['volume'])>0 for p in self.positions())+len(self.active(effect=1))>=self.c['max_positions']:reason='PORTFOLIO_LIMIT'
        q,qr=self.valid_quote(symbol,now)
        if reason is None and q is None:reason=qr
        if reason:
            self.event('OPEN_BLOCKED',symbol=symbol,evidence_id=evidence,reason=reason,tag=tag,
                evaluated_at=str(now),first_eligible_at=signal.get('first_eligible_at',signal.get('signal_at','UNKNOWN')),
                expires_at=signal.get('expires_at','UNKNOWN'),
                busy_context=self.transport.describe_busy() if reason=='BROKER_BUSY' and hasattr(self.transport,'describe_busy') else 'UNKNOWN')
            return False,reason
        nav=float(self.snapshot['cash']['nav']);available=float(self.snapshot['cash']['available'])
        direction=signal['direction'];side=1 if direction=='bull' else 2
        price=float(q['ask'] if side==1 else q['bid'])
        try:validate_anchor_proof(signal,symbol,side,price)
        except (ValueError,KeyError,TypeError):
            self.event('OPEN_BLOCKED',symbol=symbol,evidence_id=evidence,reason='INVALID_ANCHOR',tag=tag)
            return False,'INVALID_ANCHOR'
        if self.c.get('strategy_name')=='S0_SHORT_PAPER_CORE_V2':
            try:validate_execution_proof(dict(signal,price=price),stamp(now).to_pydatetime(),side)
            except (ValueError,KeyError,TypeError) as exc:
                reason='INVALID_EXECUTION_PROOF:'+str(exc)
                self.event('OPEN_BLOCKED',symbol=symbol,evidence_id=evidence,reason=reason,tag=tag)
                return False,reason
        # Structure and thresholds are unchanged; use the currently observable bounded limit reference.
        from futures_trend_quant.core.risk import build_stop_plan
        try:
            plan=build_stop_plan(direction=direction,entry=price,anchor=signal['anchor'],atr=signal['atr'],tick=q['tick'],
                buffer_atr=signal['rules']['stop_buffer_atr'],min_atr=signal['rules']['stop_min_atr'],max_atr=signal['rules']['stop_max_atr'])
        except ValueError as exc:
            self.event('OPEN_BLOCKED',symbol=symbol,evidence_id=evidence,reason='STRUCTURAL_STOP_INVALID',tag=tag,
                detail=str(exc),entry_price=price,anchor=signal['anchor'],atr=signal['atr'],tick=q['tick'],
                stop_rules=signal['rules'],quote_event_at=q['created_at'],quote_received_at=q['received_at'],evaluated_at=str(now))
            return False,'STRUCTURAL_STOP_INVALID'
        if plan.distance*signal['multiplier']>nav*self.c['risk_pct']:
            self.event('OPEN_BLOCKED',symbol=symbol,evidence_id=evidence,reason='ONE_LOT_RISK_LIMIT',tag=tag)
            return False,'ONE_LOT_RISK_LIMIT'
        margin=price*signal['multiplier']*signal['margin_ratio']
        if margin>available or max(0.,nav-available)+margin>nav*self.c['margin_usage_pct']:
            self.event('OPEN_BLOCKED',symbol=symbol,evidence_id=evidence,reason='MARGIN_LIMIT',tag=tag)
            return False,'MARGIN_LIMIT'
        if test_cancel:price=max(plan.stop+q['tick'],float(q['bid'])-2*q['tick']) if side==1 else min(plan.stop-q['tick'],float(q['ask'])+2*q['tick'])
        intent=dict(id=str(uuid.uuid4()),symbol=symbol,csymbol=csymbol,direction=direction,side=side,effect=1,price=price,
            stop=plan.stop,target=plan.target,entry_atr=signal['atr'],entry_tick=q['tick'],multiplier=signal['multiplier'],margin_ratio=signal['margin_ratio'],
            **{k:signal.get(k) for k in PROOF_FIELDS},
            **{k:signal.get(k) for k in EXECUTION_FIELDS},
            evidence_id=evidence,path=signal.get('path','CONNECTIVITY_TEST'),
            core_evidence_id=signal.get('core_evidence_id',evidence),
            trade_reason=signal.get('trade_reason',{}),
            signal_m15_index=signal.get('m15_index',0),tag=tag,test_cancel=test_cancel,force_exit='')
        self.s['used_signals'].append(evidence)
        self._send(intent,now);return True,intent['id']

    def offer(self, signal, now):
        """Accept a never-submitted proposal, preserving its original expiry."""
        evidence = signal['evidence_id']
        registry = self.s['candidate_status']
        if evidence in registry:
            return False, 'CANDIDATE_' + registry[evidence]['status']
        first = stamp(signal.get('first_eligible_at', signal['signal_at']))
        expiry = stamp(signal.get('expires_at', first + pd.Timedelta(seconds=30)))
        if expiry != first + pd.Timedelta(seconds=self.c['signal_max_age_seconds']):
            return False, 'INVALID_EVIDENCE_EXPIRY'
        # Only the first eligible closed-M15 time is the strategy signal clock.
        signal = dict(signal, signal_at=first.isoformat(), expires_at=expiry.isoformat())
        if stamp(now)>expiry or stamp(now)<first:
            status='EXPIRED_BEFORE_QUEUE' if stamp(now)>expiry else 'FUTURE_CLOCK_REJECTED'
            registry[evidence]=dict(status=status,symbol=signal['symbol'],first_eligible_at=first.isoformat(),expires_at=expiry.isoformat())
            self.save();self.event('CANDIDATE_REJECTED_AT_QUEUE',evidence_id=evidence,status=status,at=str(now))
            return False,'CANDIDATE_'+status
        q,_=self.valid_quote(signal['symbol'],now)
        if q is not None:
            signal['reference_opponent_price']=float(q['ask'] if signal['direction']=='bull' else q['bid'])
        row = dict(status='WAITING', symbol=signal['symbol'], direction=signal['direction'],
                   first_eligible_at=first.isoformat(), expires_at=expiry.isoformat(), queued_at=str(now))
        registry[evidence] = row
        self.pending[evidence] = signal
        self.save()
        self.event('CANDIDATE_QUEUED', evidence_id=evidence, **row,
                   evidence_created_at=signal.get('evidence_created_at','UNKNOWN'))
        return False, 'CANDIDATE_WAITING'  # Not an order or a promise of entry.

    def invalidate_candidates(self, symbol, reason, now, direction=None):
        for evidence, signal in list(self.pending.items()):
            if signal['symbol'] != symbol or (direction is not None and signal['direction'] != direction):
                continue
            self._finish_candidate(evidence, 'INVALIDATED', reason, now)

    def _finish_candidate(self, evidence, status, reason, now):
        self.pending.pop(evidence, None)
        self.s['candidate_status'][evidence].update(status=status, reason=reason, finished_at=str(now))
        self.save()
        self.event('CANDIDATE_FINISHED', evidence_id=evidence, status=status, reason=reason, at=str(now))

    def drain_candidates(self, now):
        # tick() must have handled exits/cancels first. An unresolved reduction
        # holds entry priority even if its quote/available quantity is missing.
        exit_due = any(not x.get('completed') for x in self.s['due_events'].values())
        exit_due = exit_due or any(m.get('force_exit') for m in self.s['meta'].values()
                                  if any(p['symbol']==m['symbol'] and int(p['volume'])>0
                                         and int(p['side'])==(1 if m['direction']=='bull' else 2) for p in self.positions()))
        for evidence, signal in list(self.pending.items()):
            if stamp(now) > stamp(signal['expires_at']) or stamp(now) < stamp(signal['signal_at']):
                self._finish_candidate(evidence, 'EXPIRED', 'ORIGINAL_DEADLINE', now);continue
            if self.s['pause_new'] or self.s['halt_reason']:
                self._finish_candidate(evidence, 'BLOCKED', self.s['halt_reason'] or 'STOP_NEW_OPEN', now);continue
            if exit_due:continue
            if 'reference_opponent_price' not in signal:
                q,_=self.valid_quote(signal['symbol'],now)
                if q is None:continue
                signal['reference_opponent_price']=float(q['ask'] if signal['direction']=='bull' else q['bid'])
                self.event('CANDIDATE_FIRST_QUOTE',evidence_id=evidence,reference_opponent_price=signal['reference_opponent_price'],at=str(now))
            ok, reason = self.open(signal, now)
            if ok:
                self._finish_candidate(evidence, 'SUBMITTED', reason, now)
                break  # One account-scoped RPC in flight at a time.
            if reason not in ('BROKER_BUSY','PENDING_RECON','QUOTE_MISSING','QUOTE_STALE_OR_FUTURE','RECEPTION_STALE'):
                self._finish_candidate(evidence, 'BLOCKED', reason, now)

    def cancel(self,intent,reason,now):
        intent['force_exit']=reason
        if not intent.get('order_id'):self.save();return
        if intent.get('cancel_requested'):
            elapsed=(stamp(now)-stamp(intent['cancel_requested_at'])).total_seconds()
            confirmed_active=self.snapshot and any(x.get('cl_ord_id')==intent['order_id'] for x in self.snapshot['active'])
            if elapsed<10 or not confirmed_active or self.last_snapshot is None or (stamp(now)-stamp(self.last_snapshot)).total_seconds()>3:
                self.save();return
        if self.transport.busy():self.save();return
        intent['cancel_attempts']=intent.get('cancel_attempts',0)+1
        if intent['cancel_attempts']>=3:self.s['halt_reason']='CANCEL_SUPERVISION_REQUIRED'
        intent['cancel_requested']=True;intent['cancel_requested_at']=str(now);self.save()
        self._request('cancel',dict(cl_ord_id=intent['order_id']),str(uuid.uuid4()))
        self.event('CANCEL_REQUESTED',order_id=intent['order_id'],reason=reason)

    def on_order(self,row,now):
        if row.get('account_id')!=self.c['account_id']:raise ValueError('foreign/missing order account')
        oid=row.get('cl_ord_id');i=next((x for x in self.s['intents'].values() if x.get('order_id')==oid),None)
        if i is None:
            # Crash after submit: bind only one exact fingerprint, never send another open.
            matches=[x for x in self.s['intents'].values() if not x.get('order_id') and x['symbol']==row.get('symbol') and
                x['side']==int(row.get('side',0)) and x['effect']==int(row.get('position_effect',0)) and
                abs(x['price']-float(row.get('price',0)))<1e-8 and int(row.get('volume',0))==1 and
                row.get('created_at') and stamp(x['submitted_at'])-pd.Timedelta(seconds=1)<=stamp(row['created_at'])<=stamp(x['submitted_at'])+pd.Timedelta(seconds=30)]
            if len(matches)!=1:raise ValueError('unknown/ambiguous order requires reconciliation')
            i=matches[0];i['order_id']=oid;self.event('ORDER_RECOVERED',intent_id=i['id'],order_id=oid)
        if row.get('symbol')!=i['symbol'] or int(row.get('side',0))!=i['side'] or int(row.get('position_effect',0))!=i['effect'] or int(row.get('volume',0))!=1:
            raise ValueError('known order identity/side/effect/volume contradiction')
        filled=int(row.get('filled_volume',0));old=int(i.get('filled',0));status=int(row.get('status',0))
        if filled<old:return  # Stale cumulative report cannot undo a fill.
        if filled>1:raise ValueError('paper order filled more than one lot')
        previous_status=i.get('status',0)
        if status not in TERMINAL and previous_status in TERMINAL:status=previous_status
        if filled>old:
            vwap=float(row.get('filled_vwap',0))
            if not math.isfinite(vwap) or vwap<=0:raise ValueError('unknown fill price')
            i['filled_vwap']=vwap
            i.setdefault('first_fill_received_at',str(now))
            q=i.get('decision_quote',{})
            if q.get('tick'):
                i['fill_vs_quoted_opponent_ticks']=(vwap-float(q['ask'] if i['side']==1 else q['bid']))*(1 if i['side']==1 else -1)/float(q['tick'])
            if previous_status in TERMINAL:i['force_exit']=i.get('force_exit') or 'LATE_FILL_AFTER_TERMINAL'
            self.event('CUMULATIVE_FILL_INCREMENT',order_id=oid,delta=filled-old,cumulative=filled,price=vwap,tag=i['tag'])
        i['filled']=filled;i['status']=status;i['last_report_at']=str(now)
        i['broker_created_at']=row.get('created_at');i['broker_updated_at']=row.get('updated_at')
        rejection=row.get('ord_rej_reason') or row.get('reject_reason') or i.get('broker_reject_reason')
        detail=row.get('ord_rej_reason_detail') or row.get('reject_reason_detail') or i.get('broker_reject_detail')
        rejection_changed=(rejection,detail)!=(i.get('broker_reject_reason'),i.get('broker_reject_detail'))
        i['broker_reject_reason']=rejection;i['broker_reject_detail']=detail
        if status!=previous_status or rejection_changed:
            if status in TERMINAL:i.setdefault('terminal_received_at',str(now))
            self.event('ORDER_STATUS',order_id=oid,intent_id=i['id'],tag=i['tag'],status=status,filled=filled,
                reject_reason=i['broker_reject_reason'],reject_detail=i['broker_reject_detail'])
        if i['effect']==1 and filled:
            key=self.key(i['symbol'],i['direction'])
            meta=self.s['meta'].get(key)
            if meta is not None and meta.get('open_intent_id')!=i['id'] and filled==old:
                self.save();return  # Old filled orders must not overwrite a later position's stop/deadline.
            if meta is None or meta.get('open_intent_id')!=i['id']:
                entry_time=row.get('updated_at') or str(now)
                day=self.cal(i['symbol']).day(entry_time)
                meta=dict(symbol=i['symbol'],direction=i['direction'],csymbol=i['csymbol'],stop=i['stop'],target=i['target'],
                    entry_price=i['filled_vwap'],entry_day=day,opened_at=entry_time,deadline=self.cal(i['symbol']).deadline(day),
                    multiplier=i['multiplier'],margin_ratio=i['margin_ratio'],tag=i['tag'],open_intent_id=i['id'],force_exit=i.get('force_exit',''))
                meta.update(entry_atr=i.get('entry_atr'),entry_tick=i.get('entry_tick'))
                if controls_enabled(self.c) and hasattr(self.cal(i['symbol']),'effective_deadline'):
                    meta['d_plus_one_deadline']=meta['deadline']
                    meta['deadline']=self.cal(i['symbol']).effective_deadline(day,entry_time)
                self.s['meta'][key]=meta
            sign=1 if i['direction']=='bull' else -1
            meta['entry_price']=i['filled_vwap'];meta['target']=meta['entry_price']+sign*2*abs(meta['entry_price']-meta['stop'])
            if sign*(meta['entry_price']-meta['stop'])<=0:meta['force_exit']='FILL_BEYOND_STRUCTURAL_STOP'
            if (i['side']==1 and i['filled_vwap']>i['price']+1e-8) or (i['side']==2 and i['filled_vwap']<i['price']-1e-8):
                meta['force_exit']='FILL_OUTSIDE_LIMIT';self.s['halt_reason']='FILL_OUTSIDE_LIMIT'
            if i.get('force_exit'):meta['force_exit']=i['force_exit']
            i['filled_protection']={k:meta.get(k) for k in ('stop','target','deadline','entry_day','opened_at','open_intent_id')}
        self.save()

    def apply_snapshot(self,snapshot,now):
        cash=snapshot['cash']
        if cash.get('account_id')!=self.c['account_id']:raise ValueError('foreign/missing cash account')
        nav=float(cash['nav']);available=float(cash['available'])
        if not math.isfinite(nav) or nav<=0 or not math.isfinite(available):raise ValueError('invalid broker cash')
        for row in snapshot['positions']+snapshot['orders']+snapshot['active']+snapshot['fills']:
            if row.get('account_id')!=self.c['account_id']:raise ValueError('foreign/missing snapshot account')
        # Valid account-scoped positions remain usable for confirmed reductions
        # even when an unrelated unknown order blocks fresh entries.
        self.snapshot=snapshot;self.last_snapshot=str(now);self.ready=False;self.block='PENDING_RECON'
        for row in snapshot['orders']:self.on_order(row,now)
        for row in snapshot['active']:
            intent=next((i for i in self.s['intents'].values() if i.get('order_id')==row.get('cl_ord_id')),None)
            if intent is None or intent.get('status') in TERMINAL:
                raise ValueError('broker active order contradicts local terminal/unknown state')
        for p in snapshot['positions']:
            volume=int(p['volume']);available_position=int(p.get('available',0))
            if volume<0 or not 0<=available_position<=volume:raise ValueError('invalid broker position quantities')
            if int(p['volume'])>0:
                direction='bull' if int(p['side'])==1 else 'bear' if int(p['side'])==2 else 'UNKNOWN'
                key=self.key(p['symbol'],direction)
                if key not in self.s['meta']:raise ValueError('unowned broker position')
                if int(p.get('covered_flag',0)) or int(p.get('hedge_flag',0)):raise ValueError('unsupported position property')
                vwap=float(p['vwap'])
                if not math.isfinite(vwap) or vwap<=0:raise ValueError('broker position entry price unknown')
                self.s['meta'][key]['entry_price']=vwap
        for x in snapshot['fills']:
            if int(x.get('exec_type',15))!=15:continue
            eid=x.get('exec_id')
            if not eid:raise ValueError('execution ID missing')
            if controls_enabled(self.c):
                commission=x.get('commission')
                if commission is None or not math.isfinite(float(commission)) or float(commission)<0:
                    raise ValueError('platform execution commission unknown')
                fee=dict(cl_ord_id=x.get('cl_ord_id'),volume=x.get('volume'),commission=float(commission))
                previous=self.s['execution_fees'].get(eid)
                if previous is not None and previous!=fee:raise ValueError('execution fee identity changed')
                self.s['execution_fees'][eid]=fee
            if eid in self.s['exec_ids']:continue
            intent=next((i for i in self.s['intents'].values() if i.get('order_id')==x.get('cl_ord_id')),None)
            if intent is None:raise ValueError('unknown execution')
            self.s['exec_ids'].append(eid)
            fields=['exec_id','cl_ord_id','symbol','side','position_effect','volume','price','commission','created_at','tag']
            record={k:x.get(k) for k in fields};record['tag']=intent['tag']
            file=self.evidence/'fills.csv';exists=file.exists()
            with file.open('a',encoding='utf8',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=fields)
                if not exists:writer.writeheader()
                writer.writerow(record);f.flush()
            self.event('EXECUTION_REPORT',execution=record)
        expected={}
        for i in self.s['intents'].values():
            key=self.key(i['symbol'],i['direction'])
            expected[key]=expected.get(key,0)+(1 if i['effect']==1 else -1)*int(i.get('filled',0))
        actual={self.key(p['symbol'],'bull' if int(p['side'])==1 else 'bear'):int(p['volume']) for p in snapshot['positions'] if int(p['volume'])>0}
        if {k:v for k,v in expected.items() if v}!={k:v for k,v in actual.items() if v}:
            raise ValueError('broker position and cumulative fills disagree; pending reconciliation')
        for i in self.s['intents'].values():
            if i['effect']!=1 and i.get('filled')==1 and actual.get(self.key(i['symbol'],i['direction']),0)==0 and not self.active(i['symbol']):
                i['closed_confirmed']=True
        day=self.calendar.day(now)
        if self.s['initial_nav'] is None:
            self.s.update(initial_nav=nav,peak_nav=nav,initial_commission=float(cash.get('cum_commission',0)))
        if self.s['day']!=day:
            self.s.update(day=day,day_nav=nav)
            if self.s['halt_reason']=='DAILY_LOSS_LIMIT':self.s['halt_reason']=''
        self.s['peak_nav']=max(float(self.s['peak_nav']),nav);self.s['last_nav']=nav
        self.s['max_drawdown_pct']=max(self.s.get('max_drawdown_pct',0.),1-nav/self.s['peak_nav'])
        if nav<=self.s['day_nav']*(1-self.c['daily_loss_pct']) and self.s['halt_reason'] in ('','DAILY_LOSS_LIMIT'):self.s['halt_reason']='DAILY_LOSS_LIMIT'
        if nav<=self.s['peak_nav']*(1-self.c['observation_drawdown_pct']):self.s['halt_reason']='OBSERVATION_DRAWDOWN_LIMIT'
        if controls_enabled(self.c):
            for trade in update_roundtrips(self.s,now,self.calendar):self.event('STRATEGY_ROUNDTRIP_CONFIRMED',**trade,cooldown=self.s['loss_cooldown'])
        unresolved=[i for i in self.active() if not i.get('order_id')]
        if unresolved:raise ValueError('submit outcome unknown; no automatic resend')
        self.snapshot=snapshot;self.last_snapshot=str(now);self.ready=True;self.block='';self.s['snapshot_count']+=1
        self.save()
        with (self.evidence/'position_recon.jsonl').open('a',encoding='utf8') as f:
            f.write(json.dumps(dict(at=str(now),nav=nav,available=available,positions=snapshot['positions'],active=snapshot['active'],
                status='RECONCILED',fees_label='GM_SIMULATED_PLATFORM_DEDUCTIONS_NOT_CERTIFIED_REAL_COST'))+'\n')

    def accept(self,job,reply,now):
        self.event('RPC_APPLIED',op=job['op'],rpc_id=job.get('id','UNKNOWN'),
                   queued_at=job.get('queued_at','UNKNOWN'),sent_at=job.get('sent_at','UNKNOWN'),
                   returned_at=reply.get('local_received_at','UNKNOWN'),applied_at=str(now),ok=bool(reply.get('ok')))
        if job['op']=='submit':
            intent=self.s['intents'][job['id']]
            intent['rpc_sent_at']=job.get('sent_at')
            intent['rpc_returned_at']=reply.get('local_received_at',str(now))
            intent['native_submit_invoked']=reply.get('native_submit_invoked',reply.get('submit_may_have_reached_broker','UNKNOWN'))
            intent['final_execution_quote']=reply.get('final_execution_quote')
            self.save()
        if not reply.get('ok'):
            if job['op']=='submit' and reply.get('submit_may_have_reached_broker') is False:
                intent=self.s['intents'][job['id']];intent.update(status=8,execution_stage='LOCAL_REJECTED_BEFORE_NATIVE',local_reject_reason=reply.get('reason'));self.save()
            self.ready=False;self.block='PENDING_RECON';self.event('RPC_FAILED',op=job['op'],rpc_id=job.get('id'),
                native_submit_invoked=reply.get('native_submit_invoked',reply.get('submit_may_have_reached_broker','UNKNOWN')),reason=reply.get('reason'))
            return
        try:
            if job['op']=='snapshot':self.apply_snapshot(reply['data'],now)
            elif job['op']=='submit':
                intent=self.s['intents'][job['id']]
                rows=reply['data']
                if len(rows)!=1 or rows[0].get('account_id')!=self.c['account_id'] or not rows[0].get('cl_ord_id'):raise ValueError('unknown submit acknowledgment')
                intent['order_id']=rows[0]['cl_ord_id'];intent['ack_received_at']=str(now);intent['execution_stage']='BROKER_ACKNOWLEDGED';self.save();self.on_order(rows[0],now)
                self.ready=False;self.last_snapshot=None;self.block='POST_ORDER_SNAPSHOT_REQUIRED'
            elif job['op']=='cancel':
                self.ready=False;self.last_snapshot=None;self.block='POST_CANCEL_SNAPSHOT_REQUIRED'
            # cancel response is an intent acknowledgment only; only snapshot/order state confirms completion.
        except Exception as exc:
            self.ready=False;self.block='PENDING_RECON';self.event('RECON_FAILED',error=type(exc).__name__,reason=str(exc))

    def force_exit(self,symbol,reason,now,direction=None,context=None):
        for key,meta in self.s['meta'].items():
            if meta['symbol']==symbol and (direction is None or meta['direction']!=direction):
                meta['force_exit']=reason
                if context is not None:meta['exit_evidence']=context
        for i in self.active(symbol,effect=1):
            if direction is None or i['direction']!=direction:self.cancel(i,reason,now)
        self.save()

    def m1(self,bar,now):
        for evidence,signal in list(self.pending.items()):
            if signal['symbol']!=bar.symbol or stamp(bar.eob)<=stamp(signal['signal_at']):continue
            if (bar.low<float(signal['anchor']) if signal['direction']=='bull' else bar.high>float(signal['anchor'])):
                self._finish_candidate(evidence,'INVALIDATED','ANCHOR_BROKEN_M1_WHILE_WAITING',now)
        for meta in self.s['meta'].values():
            if meta['symbol']!=bar.symbol or stamp(bar.bob)<stamp(meta['opened_at']):continue
            live=any(p['symbol']==meta['symbol'] and int(p['side'])==(1 if meta['direction']=='bull' else 2) and int(p['volume'])>0 for p in self.positions())
            live=live or sum((1 if i['effect']==1 else -1)*int(i.get('filled',0)) for i in self.s['intents'].values()
                            if i['symbol']==meta['symbol'] and i['direction']==meta['direction'])>0
            if not live:continue
            if controls_enabled(self.c):
                shadow=observe_shadow(meta,bar,now,self.cal(bar.symbol))
                if shadow:self.event(shadow.pop('kind'),symbol=bar.symbol,open_intent_id=meta['open_intent_id'],execution_enabled=False,**shadow)
            sign=1 if meta['direction']=='bull' else -1
            stop=bar.low<=meta['stop'] if sign==1 else bar.high>=meta['stop']
            target=bar.high>=meta['target'] if sign==1 else bar.low<=meta['target']
            if stop or target:
                meta['force_exit']='STOP_M1_TRIGGER' if stop else 'TARGET_M1_TRIGGER'
                if stop and target:self.event('M1_PATH_UNKNOWN_BOTH_TOUCHED',symbol=bar.symbol)
        self.save()

    def tick(self,now):
        if self.transport.hung():self.ready=False;self.block='PENDING_RECON'
        for i in list(self.active()):
            block=self.cal(i['symbol']).entry_block(now)
            age=(stamp(now)-stamp(i['submitted_at'])).total_seconds()
            risk_block=self.s['halt_reason'] or (cooldown_reason(self.s,now,self.calendar) if controls_enabled(self.c) else None)
            if i['effect']==1 and (block or age>=self.c['open_wait_seconds'] or self.s['pause_new'] or risk_block or i.get('force_exit') or (i.get('test_cancel') and age>=1)):
                self.cancel(i,risk_block or block or i.get('force_exit') or ('STOP_NEW_OPEN' if self.s['pause_new'] else 'OPEN_WAIT_EXPIRED'),now)
            elif i['effect']!=1 and age>=10:self.cancel(i,i.get('reason','CLOSE_WAIT_EXPIRED'),now)
        for p in self.positions():
            qty=int(p['volume'])
            if qty<=0:continue
            direction='bull' if int(p['side'])==1 else 'bear';key=self.key(p['symbol'],direction)
            meta=self.s['meta'].get(key)
            if meta is None:continue
            cal=self.cal(p['symbol'])
            due=cal.due(meta['entry_day'],now)
            if due:meta['force_exit']=due[1]
            q,why=self.valid_quote(p['symbol'],now)
            if q is not None:
                price=float(q.get('last',q['bid']));sign=1 if direction=='bull' else -1
                if sign*(price-meta['stop'])<=0:meta['force_exit']='STOP_TICK_TRIGGER'
                elif sign*(price-meta['target'])>=0:meta['force_exit']='TARGET_TICK_TRIGGER'
            reason=meta.get('force_exit')
            if not reason:continue
            due_id=key+'|'+meta['open_intent_id']
            if due_id not in self.s['due_events']:
                self.s['due_events'][due_id]=dict(symbol=p['symbol'],direction=direction,first_due_at=str(now),reason=reason,completed=False,remaining=qty)
                self.event('EXIT_DUE',symbol=p['symbol'],direction=direction,reason=reason,quantity=qty)
            self.s['due_events'][due_id]['remaining']=qty;self.save()
            if int(p.get('covered_flag',0)) or int(p.get('hedge_flag',0)):continue
            owned=sum((1 if i['effect']==1 else -1)*int(i.get('filled',0)) for i in self.s['intents'].values()
                if i['symbol']==p['symbol'] and i['direction']==direction)
            if owned<1:
                if not meta.get('unowned_residual_logged'):
                    self.event('UNOWNED_RESIDUAL_RISK_REQUIRES_RECON',symbol=p['symbol'],direction=direction,remaining=qty)
                    meta['unowned_residual_logged']=True
                continue
            wait=('OUTSIDE_SESSION' if not cal.market_open(now) else why if q is None else 'BROKER_BUSY' if self.transport.busy()
                  else 'PENDING_RECON' if self.last_snapshot is None or (stamp(now)-stamp(self.last_snapshot)).total_seconds()>3
                  else 'CLOSE_ORDER_ACTIVE' if self.active(p['symbol'],effect=2) else 'AVAILABLE_POSITION_ZERO' if int(p.get('available',0))<1 else None)
            if wait:
                event=self.s['due_events'][due_id]
                if event.get('wait_reason')!=wait:
                    event.update(wait_reason=wait,wait_changed_at=str(now))
                    self.event('EXIT_WAIT',symbol=p['symbol'],reason=wait,at=str(now),last_snapshot=self.last_snapshot,
                        busy_context=self.transport.describe_busy() if hasattr(self.transport,'describe_busy') else 'UNKNOWN')
                continue
            attempts=meta.get('close_attempts',0)
            if meta.get('last_close_attempt_at') and (stamp(now)-stamp(meta['last_close_attempt_at'])).total_seconds()<min(30,2**min(attempts,5)):continue
            side=2 if direction=='bull' else 1;limit=float(q['bid'] if side==2 else q['ask'])
            try:effect=close_effect(p['symbol'],p)
            except ValueError:
                self.ready=False;self.block='CLOSE_BUCKET_UNKNOWN';continue
            intent=dict(id=str(uuid.uuid4()),symbol=p['symbol'],csymbol=meta['csymbol'],direction=direction,side=side,effect=effect,
                price=limit,multiplier=meta['multiplier'],margin_ratio=meta['margin_ratio'],evidence_id='CLOSE:'+key,
                reason=reason,tag=meta['tag'],path='EXIT',entry_price=meta['entry_price'],initial_stop=meta['stop'],
                open_intent_id=meta['open_intent_id'],
                initial_target=meta['target'],deadline=meta['deadline'],
                exit_evidence=meta.get('exit_evidence',{}),
                entry_day=meta['entry_day'],opened_at=meta['opened_at'])
            meta['close_attempts']=attempts+1;meta['last_close_attempt_at']=str(now)
            self._send(intent,now)
        for key,event in self.s['due_events'].items():
            if event['completed']:continue
            symbol=event['symbol'];direction=event['direction'];side=1 if direction=='bull' else 2
            remaining=sum(int(p['volume']) for p in self.positions() if p['symbol']==symbol and int(p['side'])==side)
            # Flat alone is insufficient while any open can still fill.
            broker_active=any(o.get('symbol')==symbol for o in (self.snapshot or {}).get('active',[]))
            if self.ready and self.last_snapshot and (stamp(now)-stamp(self.last_snapshot)).total_seconds()<=3 and remaining==0 and not self.active(symbol) and not broker_active:
                if not event['completed']:self.event('EXIT_FLAT_CONFIRMED',symbol=symbol,direction=direction)
                event.update(completed=True,remaining=0,confirmed_at=str(now))
        self.save()

    def summary(self):
        cash={} if self.snapshot is None else self.snapshot['cash']
        nav=self.s['last_nav'];initial=self.s['initial_nav'];commission=None
        if self.s['initial_commission'] is not None and 'cum_commission' in cash:commission=float(cash['cum_commission'])-self.s['initial_commission']
        return dict(status='RECONCILED' if self.ready else 'PENDING_RECON',block=self.block,nav=nav,
            platform_equity_change=None if nav is None or initial is None else nav-initial,
            platform_simulated_deductions=commission,costs_certified=False,positions=self.positions(),
            active_intents=len(self.active()),halt_reason=self.s['halt_reason'],pause_new=self.s['pause_new'],
            execution_policy_version=self.c.get('execution_policy_version'),loss_cooldown=self.s.get('loss_cooldown',{}),
            new_entry_cooldown_reason=cooldown_reason(self.s,datetime.now(timezone.utc),self.calendar) if controls_enabled(self.c) else None,
            confirmed_strategy_roundtrips=len(self.s.get('strategy_roundtrips',{})),shadow_exits_execution_enabled=False,
            max_drawdown_pct_from_platform_nav_polls=self.s.get('max_drawdown_pct',0.),
            position_plans={k:m for k,m in self.s['meta'].items() if any(p['symbol']==m['symbol'] and int(p['side'])==(1 if m['direction']=='bull' else 2) and int(p['volume'])>0 for p in self.positions())},
            connectivity_stage=self.s['connectivity_stage'],connectivity_passed=self.s['connectivity_passed'],
            due_events=self.s['due_events'],independent_profitability_proven=False)
