"""Isolated holding-policy account. Frozen V8 account/config remain unchanged."""
from collections import Counter
import numpy as np
from tools.full_trade_v8.engine import Account,MINUTE,iso,intrabar_exit
from .policy import HoldingCalendar


class ShortSwingAccount(Account):
    def __init__(self,data,group,cost_model=None,slip_ticks=0,policy=None):
        super().__init__(data,group,cost_model,slip_ticks)
        self.policy=HoldingCalendar(data.get('calendar_clocks',data['clocks']),**policy) if policy else None
        self.requests={};self.exit_attempts=[];self.force_meta={};self._now=None

    def enter(self,leg,intent,t,day,bar):
        if self.policy:
            reason=self.policy.entry_block(leg['cs'],day,t-MINUTE)
            if reason or any(p.cs==leg['cs'] and p.id in self.requests for p in self.positions):
                self.blocks[reason or 'PENDING_CLOSE_NO_REOPEN']+=1;return
        super().enter(leg,intent,t,day,bar)

    def decide(self,leg,row,t):
        super().decide(leg,row,t)
        if self.policy:
            day=leg['daymap'][t]
            if self.policy.entry_block(leg['cs'],day,t) and leg['symbol'] in self.pending:
                del self.pending[leg['symbol']];self.blocks['CLOSE_WINDOW_SIGNAL_CANCELLED']+=1

    def request(self,p,at,reason):
        if p.id not in self.requests:
            self.requests[p.id]=dict(requested_at=int(at),reason=reason,first_blocked_at=None,attempts=0)

    def attempt(self,p,t,day,price,reason,double=False):
        # Only explicit, contemporaneous execution evidence blocks a proxy fill.
        # Missing evidence is NOT proof of executable liquidity and is labelled.
        snapshot=self.data.get('execution_snapshots',{}).get((p.symbol,int(t)))
        blocked=None if snapshot is None else snapshot.get('blocked_reason')
        if blocked:
            self.request(p,t,reason);r=self.requests[p.id];r['attempts']+=1
            if r['first_blocked_at'] is None:r['first_blocked_at']=t
            self.exit_attempts.append(dict(trade_id=p.id,symbol=p.symbol,time=iso(t),reason=reason,
                status='NOT_FILLED_EXPOSURE_RETAINED',blocked_reason=blocked,lots_remaining=p.qty))
            return False
        r=self.requests.get(p.id)
        super().close(p,t,day,price,reason,double)
        self.trades[-1].update(exit_execution_basis='M1_OPEN_OR_LEVEL_PROXY_UNVERIFIED' if snapshot is None else 'SUPPLIED_EXECUTION_SNAPSHOT_PROXY',
            discipline_requested_at=iso(r['requested_at']) if r else None,
            discipline_reason=r['reason'] if r else None,
            first_blocked_exit_at=iso(r['first_blocked_at']) if r and r['first_blocked_at'] is not None else None,
            first_model_exit_at=iso(t),blocked_attempts=r['attempts'] if r else 0)
        self.requests.pop(p.id,None);return True

    def run(self):
        if self.policy is None:return super().run()
        legs=self.data['legs'];timeline=sorted(set(int(t) for c in self.data['clocks'].values() for t in c['ns']))
        if not timeline:raise ValueError('empty scoring clock')
        for t in timeline:
            active={s:(l,l['index'].get(t),l['daymap'][t]) for s,l in legs.items() if t in l['daymap']}
            day=max(x[2] for x in active.values())
            if self.day!=day:self.day=day;self.day_start=self.nav();self.day_realized.clear()
            for s,(l,i,d) in active.items():
                if i is not None:self.marks[s]=float(l['values'][i][0]);self.mark_times[s]=t-MINUTE
            for p in list(self.positions):
                due=self.policy.due(p,t-MINUTE)
                if due:self.request(p,*due)
                if p.symbol not in active:continue
                l,i,d=active[p.symbol]
                if i is None:
                    if p.gap_first is None:p.gap_first=t
                    if p.id in self.requests:
                        r=self.requests[p.id];r['attempts']+=1
                        if r['first_blocked_at'] is None:r['first_blocked_at']=t-MINUTE
                    continue
                bar=l['values'][i];reason=None
                # Preserve V8 priorities for already known exits and opening gaps.
                if p.gap_first is not None:reason='DATA_GAP_NEXT_OBSERVED_OPEN'
                elif p.exit_pending:reason='TREND_EXIT'
                elif p.sign*(bar[0]-p.stop)<=0:reason='STOP_GAP'
                elif p.sign*(bar[0]-p.target)>=0:reason='TARGET_GAP'
                elif p.id in self.requests:reason=self.requests[p.id]['reason']
                if reason:self.attempt(p,t-MINUTE,d,float(bar[0]),reason)
            for s in sorted(list(self.pending)):
                intent=self.pending[s]
                if intent['at']!=t:continue
                del self.pending[s];l,i,d=active[s]
                if i is None:self.blocks['MISSING_ENTRY_MINUTE']+=1;continue
                self.enter(l,intent,t,d,l['values'][i])
            for p in list(self.positions):
                if p.symbol not in active:continue
                l,i,d=active[p.symbol]
                if i is None or p.id in self.requests:continue
                matched=intrabar_exit(p.sign,p.stop,p.target,l['values'][i])
                if matched:self.attempt(p,t,d,*matched[:2],double=matched[2])
            for s,(l,i,d) in active.items():
                if i is not None:self.marks[s]=float(l['values'][i][3]);self.mark_times[s]=t
            self.curve.append(dict(group=self.group,eob=iso(t),trading_date=day,equity=self.nav(),cash=self.cash,
                margin=self.margin(),open_positions=len(self.positions),pending_exit_positions=len(self.requests)))
            for s,(l,i,d) in sorted(active.items()):
                row=l['facts'].get(t)
                if row is not None:self.decide(l,row,t)
        last=timeline[-1];opened=[]
        for p in self.positions:
            request=self.requests.get(p.id);deadline=self.policy.products[p.cs]['deadlines'].get(p.entry_day)
            opened.append(dict(group=self.group,trade_id=p.id,symbol=p.symbol,csymbol=p.cs,lots=p.qty,direction=p.direction,
                entry_bob=iso(p.opened),entry_price=p.entry,last_mark=self.marks[p.symbol],last_mark_eob=iso(self.mark_times[p.symbol]),
                mark_stale_calendar_minutes=(last-self.mark_times[p.symbol])/MINUTE,
                unrealized_pnl=(self.marks[p.symbol]-p.entry)*p.sign*p.mult*p.qty,paid_entry_fee=p.entry_fee,
                unrealized_net_of_paid_entry_fee=(self.marks[p.symbol]-p.entry)*p.sign*p.mult*p.qty-p.entry_fee,
                estimated_unexecuted_exit_fee=self.fee(p.cs,self.marks[p.symbol],p.qty,p.mult,'today' if p.entry_day==self.day else 'close'),
                entry_bar_one_price=p.entry_bar_one_price,entry_bar_zero_volume=p.entry_bar_zero_volume,
                stop_level_pnl=(p.stop-p.entry)*p.sign*p.mult*p.qty,target_level_pnl=(p.target-p.entry)*p.sign*p.mult*p.qty,
                unresolved_data_gap=p.gap_first is not None,remaining_gap_risk='UNKNOWN_NOT_ZERO',
                discipline_deadline=None if deadline is None else iso(deadline),pending_exit=request is not None,
                requested_exit_at=None if request is None else iso(request['requested_at'])))
        return dict(trades=self.trades,curve=self.curve,opened=opened,blocks=dict(self.blocks),events=self.events,
            dynamic_c_reselections=self.dynamic_c,exit_attempts=self.exit_attempts,
            execution_certified=False,missing_quote_evidence='M1_PROXY_ONLY; ACTUAL_FLAT_NOT_PROVED')
