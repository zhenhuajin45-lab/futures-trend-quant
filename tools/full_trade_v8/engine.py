"""Research account ledger. No GM SDK, broker state, orders or future labels."""
from collections import Counter,defaultdict
from dataclasses import dataclass
import math
import numpy as np
import pandas as pd
from futures_trend_quant.core.risk import build_stop_plan
from futures_trend_quant.core.entry.cooldown import evaluate_cooldown

MINUTE=60_000_000_000


def iso(t):
    return pd.Timestamp(int(t),tz='UTC').tz_convert('Asia/Shanghai').isoformat()


def intrabar_exit(sign,stop,target,bar):
    o,h,l,c,v=bar
    if sign*(o-stop)<=0:return float(o),'STOP_GAP',False
    if sign*(o-target)>=0:return float(o),'TARGET_GAP',False
    hs=l<=stop if sign==1 else h>=stop
    ht=h>=target if sign==1 else l<=target
    return (float(stop),'STOP',bool(ht)) if hs else (float(target),'TARGET',False) if ht else None


@dataclass
class Holding:
    id:int;symbol:str;cs:str;direction:str;sign:int;qty:int;entry:float;stop:float;target:float
    risk:float;mult:float;tick:float;opened:int;entry_day:str;path:str;evidence:str;signal:int
    entry_fee:float=0.;gap_first:int|None=None;exit_pending:bool=False;double_touch:bool=False


class Account:
    def __init__(self,data,group,cost_model=None,slip_ticks=0):
        self.data=data;self.group=group;self.config=data['config'];self.rules=self.config['rules'];self.risk=self.config['risk']
        self.products={x['csymbol']:x for x in self.config['universe']};self.specs=data['fees']['products']
        self.cash=float(self.config['backtest']['initial_cash']);self.positions=[];self.pending={};self.used=set();self.last_fill={}
        self.marks={};self.mark_times={};self.day=None;self.day_start=self.cash;self.day_realized=defaultdict(float)
        self.trades=[];self.events=[];self.curve=[];self.blocks=Counter();self.ids=0;self.dynamic_c=0;self._m5={}
        self.cost_model=cost_model;self.slip_ticks=slip_ticks

    def fee(self,cs,price,qty,mult,offset):
        if self.cost_model is None:return 0.
        rule=self.cost_model['products'][cs.split('.')[1]]
        return qty*(price*mult*rule.get(offset+'_rate',0)+rule.get(offset+'_per_lot',0)+self.cost_model['broker_add_per_lot_per_side'])

    def nav(self):
        return self.cash+sum((self.marks.get(p.symbol,p.entry)-p.entry)*p.sign*p.mult*p.qty for p in self.positions)

    def margin(self):
        return sum(self.marks.get(p.symbol,p.entry)*p.mult*p.qty*.20 for p in self.positions)

    def close(self,p,t,day,price,reason,double=False):
        raw_exit=price;price=price-p.sign*self.slip_ticks*p.tick
        fee=self.fee(p.cs,price,p.qty,p.mult,'today' if p.entry_day==day else 'close')
        gross=(price-p.entry)*p.sign*p.mult*p.qty;self.cash+=gross-fee;self.day_realized[p.cs]+=gross-fee
        row=dict(group=self.group,trade_id=p.id,csymbol=p.cs,symbol=p.symbol,direction=p.direction,
            lots=p.qty,signal_eob=iso(p.signal),entry_bob=iso(p.opened),exit_time=iso(t),
            entry_price=p.entry,exit_price=float(price),stop=p.stop,target=p.target,path=p.path,
            entry_day=p.entry_day,exit_day=day,close_today=p.entry_day==day,reason=reason,
            gross_pnl=float(gross),initial_risk_money=p.risk*p.mult*p.qty,gross_r=float(gross/(p.risk*p.mult*p.qty)),
            double_touch=bool(double),had_data_gap=p.gap_first is not None,
            first_gap_eob=None if p.gap_first is None else iso(p.gap_first),
            stop_level_scenario_pnl=(p.stop-p.entry)*p.sign*p.mult*p.qty,
            target_level_scenario_pnl=(p.target-p.entry)*p.sign*p.mult*p.qty,
            one_tick_each_side_cost=2*p.tick*p.mult*p.qty,
            break_even_all_in_cost_currency=float(gross),fees_verified=False,
            modeled_fee=p.entry_fee+fee,modeled_net_pnl=gross-p.entry_fee-fee,
            modeled_net_r=(gross-p.entry_fee-fee)/(p.risk*p.mult*p.qty),raw_exit_proxy=raw_exit,
            modeled_slippage_ticks_per_side=self.slip_ticks,execution_liquidity='M1_PROXY_NOT_ORDERBOOK_VERIFIED')
        self.trades.append(row);self.positions.remove(p)

    def _c_reselect(self,leg,row,t,same):
        """Only C formation depends on holdings. Rebuild that branch with closed bars."""
        from backtest.aggregation import aggregate_bars
        from futures_trend_quant.data.gm_bars import bars_from_records
        from futures_trend_quant.core.entry import build_entry_evidence,evaluate_entries,EntryContext
        from futures_trend_quant.core.pipeline.step import _entry_settings
        symbol=leg['symbol'];core=row['core'];entry=core['entry_evaluation']
        if symbol not in self._m5:
            bars=bars_from_records(leg['frame'].to_dict('records'))
            self._m5[symbol]=(bars,aggregate_bars(bars,5,'300s'))
        m1,m5=self._m5[symbol];end=pd.Timestamp(t,tz='UTC').tz_convert('Asia/Shanghai')
        a=[b for b in m5 if b.eob<=end][-1200:];b=[b for b in m1 if b.eob<=end][-1200:]
        d=core['trend_decision']['state']['trend_state'];last=self.last_fill.get((leg['cs'],d))
        evidence=build_entry_evidence(a,b,d,div_mode=self.rules['div_mode'],m5_lookback=self.rules['m5_flat_lookback'],
            flat_ratio=self.rules['m5_flat_slope_ratio'],position_atr=self.rules['evidence_position_atr'],
            pullback_atr=self.rules['pullback_min_atr'],consolidation_bars=self.rules['consolidation_bars'],
            consolidation_range_atr=self.rules['consolidation_range_atr'],m1_lookback=self.rules['m1_evidence_lookback'])
        context=EntryContext(direction=d,trend_confirmed=True,h1_state=entry['h1_absolute'],
            m15_slope_state=core['slope_state'],m15_reverse_divergence=core['top_div'] if d=='bull' else core['bottom_div'],
            gate_open=entry['gate_open'],same_direction_positions=same,m15_index=self.ordinal(leg,t),
            current_price=core['close'],atr14=core['atr14'],last_same_direction_fill_index=None if last is None else last[0],
            last_same_direction_fill_price=None if last is None else last[1],used_evidence_ids=frozenset(self.used))
        result=evaluate_entries(context,evidence,_entry_settings(self.rules));self.dynamic_c+=1
        return dict(accepted=result.accepted,path=result.path,evidence_id=result.evidence_id,reason=result.reason)

    def ordinal(self,leg,t):
        # Exchange-clock ordinal, consistent across actual contracts in one product.
        return int(np.searchsorted(leg['expected'],t)//15)

    def decide(self,leg,row,t):
        core=row['core'];direction=core['trend_decision']['state']['trend_state'];local=[p for p in self.positions if p.symbol==leg['symbol']]
        if core['trend_decision']['confirmed']:
            for p in local:
                if p.direction!=direction:p.exit_pending=True
        if not row['eligible']:return
        if direction not in ('bull','bear'):self.blocks['NO_DIRECTION']+=1;return
        if any(p.direction!=direction for p in local):self.blocks['OPPOSITE_EXIT_PENDING']+=1;return
        e=core.get('entry_evaluation')
        if not e or not e.get('anchor'):self.blocks['NO_ANCHOR']+=1;return
        path=e.get('path');evidence=e.get('evidence_id');same=len(local)
        if self.group=='S0':
            if not e.get('accepted'):self.blocks['TECHNICAL_'+str(e.get('reason'))]+=1;return
            if same and path=='C':
                if len(e['formed_paths'])==1:self.blocks['SAME_DIRECTION_POSITION']+=1;return
                dynamic=self._c_reselect(leg,row,t,same)
                if not dynamic['accepted']:self.blocks[dynamic['reason']]+=1;return
                path=dynamic['path'];evidence=dynamic['evidence_id']
        else:path='D';evidence='ORDINARY|'+leg['symbol']+'|'+str(t)
        if same and path!='D':self.blocks['SAME_DIRECTION_POSITION']+=1;return
        if evidence in self.used:self.blocks['EVIDENCE_USED']+=1;return
        if same>=2:self.blocks['STACK_LIMIT']+=1;return
        last=self.last_fill.get((leg['cs'],direction));index=self.ordinal(leg,t)
        cool=evaluate_cooldown(path=path,direction=direction,current_index=index,current_price=core['close'],
            atr14=core['atr14'],last_fill_index=None if last is None else last[0],last_fill_price=None if last is None else last[1],
            cooldown_bars=self.rules['cooldown_bars'],path_d_exception_atr=self.rules['path_d_cooldown_atr'])
        if not cool.allowed:self.blocks['COOLDOWN']+=1;return
        try:
            spec=self.specs[leg['cs'].split('.')[1]]
            plan=build_stop_plan(direction=direction,entry=core['close'],anchor=e['anchor']['anchor_price'],
                atr=core['atr14'],tick=spec['price_tick'],buffer_atr=self.rules['stop_buffer_atr'],
                min_atr=self.rules['stop_min_atr'],max_atr=self.rules['stop_max_atr'])
        except ValueError:self.blocks['STRUCTURAL_STOP_INVALID']+=1;return
        nxt=np.searchsorted(leg['expected'],t,side='right')
        if nxt==len(leg['expected']):self.blocks['NO_NEXT_ENTRY_CLOCK']+=1;return
        self.pending[leg['symbol']]=dict(at=int(leg['expected'][nxt]),signal=t,cs=leg['cs'],direction=direction,
            stop=plan.stop,atr=core['atr14'],path=path,evidence=evidence,index=index)

    def enter(self,leg,intent,t,day,bar):
        cs=leg['cs'];product=self.products[cs];local=[p for p in self.positions if p.cs==cs]
        if any(p.symbol!=leg['symbol'] for p in local):self.blocks['ROLLED_OLD_LEG_HELD']+=1;return
        if len(local)>=2 or (local and intent['path']!='D'):self.blocks['PRODUCT_POSITION_CAP']+=1;return
        if len(self.positions)>=5:self.blocks['PORTFOLIO_POSITION_CAP']+=1;return
        if sum(self.products[p.cs]['cluster']==product['cluster'] for p in self.positions)>=2:self.blocks['CLUSTER_CAP']+=1;return
        nav=self.nav();open_risk=sum(p.risk*p.mult*p.qty for p in self.positions)
        if max(0,self.day_start-nav)+open_risk>=self.day_start*.06:self.blocks['DAILY_PORTFOLIO_RISK']+=1;return
        if max(0,-self.day_realized[cs])+sum(p.risk*p.mult*p.qty for p in local)>=self.day_start*.02:
            self.blocks['DAILY_PRODUCT_RISK']+=1;return
        o,h,l,c,vol=bar
        if vol<=0 or o==h==l==c:self.blocks['UNVERIFIABLE_ONE_PRICE_OR_ZERO_VOLUME_ENTRY']+=1;return
        sign=1 if intent['direction']=='bull' else -1;r=sign*(o-intent['stop'])
        if r<=0:self.blocks['OPEN_THROUGH_STOP']+=1;return
        spec=self.specs[cs.split('.')[1]];tick=spec['price_tick'];mult=spec['multiplier']
        o=o+sign*self.slip_ticks*tick;r=sign*(o-intent['stop'])
        adv=leg['adv'].get(day)
        if adv is None:self.blocks['LAGGED_ADV_NOT_COMPLETE']+=1;return
        cushion=max(3*tick,o*.001)
        qty=min(math.floor(max(0,nav)*.01/((r+cushion)*mult)),
            math.floor(max(0,.8*nav-self.margin())/(o*mult*.2)),math.floor(adv*.001))
        if qty<=0:self.blocks['CAPITAL_OR_RISK_OR_ADV_ZERO_LOT']+=1;return
        target=(math.ceil((o+2*r)/tick-1e-10)*tick if sign==1 else math.floor((o-2*r)/tick+1e-10)*tick)
        self.ids+=1;p=Holding(self.ids,leg['symbol'],cs,intent['direction'],sign,qty,float(o),intent['stop'],target,
            float(r),mult,tick,t-MINUTE,day,intent['path'] if self.group=='S0' else 'ORDINARY',intent['evidence'],intent['signal'])
        self.positions.append(p);self.used.add(intent['evidence']);self.last_fill[(cs,p.direction)]=(intent['index'],float(o))
        p.entry_fee=self.fee(cs,float(o),qty,mult,'open');self.cash-=p.entry_fee;self.day_realized[cs]-=p.entry_fee
        self.events.append(dict(group=self.group,time=iso(t-MINUTE),symbol=p.symbol,kind='OPEN',lots=qty,
            equity_before=nav,margin_after=self.margin(),initial_risk=p.risk*mult*qty))

    def run(self):
        legs=self.data['legs'];timeline=sorted(set(int(t) for c in self.data['clocks'].values() for t in c['ns']))
        for t in timeline:
            active={s:(l,l['index'].get(t),l['daymap'][t]) for s,l in legs.items() if t in l['daymap']}
            day=max(x[2] for x in active.values())
            if self.day!=day:self.day=day;self.day_start=self.nav();self.day_realized.clear()
            # Opening snapshots only: intraminute exits cannot finance orders at that same earlier open.
            for s,(l,i,d) in active.items():
                if i is not None:self.marks[s]=float(l['values'][i][0]);self.mark_times[s]=t-MINUTE
            for p in list(self.positions):
                if p.symbol not in active:continue
                l,i,d=active[p.symbol]
                if i is None:
                    if p.gap_first is None:p.gap_first=t
                    continue
                bar=l['values'][i]
                if p.gap_first is not None:self.close(p,t-MINUTE,d,float(bar[0]),'DATA_GAP_NEXT_OBSERVED_OPEN')
                elif p.exit_pending:self.close(p,t-MINUTE,d,float(bar[0]),'TREND_EXIT')
                elif p.sign*(bar[0]-p.stop)<=0:self.close(p,t-MINUTE,d,float(bar[0]),'STOP_GAP')
                elif p.sign*(bar[0]-p.target)>=0:self.close(p,t-MINUTE,d,float(bar[0]),'TARGET_GAP')
            for s in sorted(list(self.pending)):
                intent=self.pending[s]
                if intent['at']!=t:continue
                del self.pending[s];l,i,d=active[s]
                if i is None:self.blocks['MISSING_ENTRY_MINUTE']+=1;continue
                self.enter(l,intent,t,d,l['values'][i])
            for p in list(self.positions):
                if p.symbol not in active:continue
                l,i,d=active[p.symbol]
                if i is None:continue
                matched=intrabar_exit(p.sign,p.stop,p.target,l['values'][i])
                if matched:self.close(p,t,d,*matched[:2],double=matched[2])
            for s,(l,i,d) in active.items():
                if i is not None:self.marks[s]=float(l['values'][i][3]);self.mark_times[s]=t
            self.curve.append(dict(group=self.group,eob=iso(t),trading_date=day,equity=self.nav(),cash=self.cash,
                margin=self.margin(),open_positions=len(self.positions)))
            for s,(l,i,d) in sorted(active.items()):
                row=l['facts'].get(t)
                if row is not None:self.decide(l,row,t)
        last=timeline[-1];opened=[]
        for p in self.positions:
            opened.append(dict(group=self.group,trade_id=p.id,symbol=p.symbol,csymbol=p.cs,lots=p.qty,direction=p.direction,
                entry_bob=iso(p.opened),entry_price=p.entry,last_mark=self.marks[p.symbol],last_mark_eob=iso(self.mark_times[p.symbol]),
                mark_stale_calendar_minutes=(last-self.mark_times[p.symbol])/MINUTE,
                unrealized_pnl=(self.marks[p.symbol]-p.entry)*p.sign*p.mult*p.qty,
                stop_level_pnl=(p.stop-p.entry)*p.sign*p.mult*p.qty,target_level_pnl=(p.target-p.entry)*p.sign*p.mult*p.qty,
                unresolved_data_gap=p.gap_first is not None,remaining_gap_risk='UNKNOWN_NOT_ZERO' if p.gap_first else 'OVERNIGHT_AND_EXECUTION_RISK_NOT_MARKED_OUT'))
        return dict(trades=self.trades,curve=self.curve,opened=opened,blocks=dict(self.blocks),events=self.events,
            dynamic_c_reselections=self.dynamic_c)
