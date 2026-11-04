"""Price-independent holding deadlines from the authorized expected session clock."""
from bisect import bisect_right
import numpy as np
import pandas as pd
from tools.full_trade_v8.engine import MINUTE


class HoldingCalendar:
    def __init__(self,clocks,buffer_minutes=5,weekend_mode='DAY_CLOSE'):
        if buffer_minutes!=5 or weekend_mode not in ('DAY_CLOSE','LAST_SESSION'):
            raise ValueError('unregistered holding policy')
        self.mode=weekend_mode;self.buffer=buffer_minutes*MINUTE;self.products={}
        for cs,clock in clocks.items():
            ns=np.asarray(clock['ns'],dtype=np.int64);days=list(clock['days'])
            if len(ns)!=len(days) or not len(ns) or (np.diff(ns)<=0).any():raise ValueError('invalid session clock')
            local=pd.to_datetime(ns,utc=True).tz_convert('Asia/Shanghai')
            daytime=[int(t) for t,s in zip(ns,local) if 9<=s.hour<16]
            closes={}
            for t,d,s in zip(ns,days,local):
                if 9<=s.hour<16:closes[d]=int(t)
            ordered=sorted(closes);deadline={d:(closes[ordered[i+1]]-self.buffer if i+1<len(ordered) else None) for i,d in enumerate(ordered)}
            blackouts=[]
            for i in np.flatnonzero(np.diff(ns)-MINUTE>=24*60*MINUTE):
                end=int(ns[i]);resume=int(ns[i+1])-MINUTE
                if weekend_mode=='DAY_CLOSE':
                    j=bisect_right(daytime,end)-1
                    if j<0:raise ValueError('no daytime close before long closure')
                    end=daytime[j]
                start=end-self.buffer
                if blackouts and start<=blackouts[-1][1]:blackouts[-1]=(blackouts[-1][0],max(resume,blackouts[-1][1]))
                else:blackouts.append((start,resume))
            self.products[cs]=dict(deadlines=deadline,day_closes=closes,blackouts=blackouts,
                starts=[x[0] for x in blackouts])

    def blackout(self,cs,bob):
        p=self.products[cs];i=bisect_right(p['starts'],bob)-1
        return p['blackouts'][i] if i>=0 and bob<p['blackouts'][i][1] else None

    def entry_block(self,cs,day,bob):
        if self.blackout(cs,bob):return 'PRE_LONG_CLOSURE_NO_REOPEN'
        close=self.products[cs]['day_closes'].get(day)
        if close is not None and close-self.buffer<=bob<close:return 'DAILY_CLOSE_WINDOW_NO_ENTRY'
        return None

    def due(self,position,bob):
        deadline=self.products[position.cs]['deadlines'].get(position.entry_day)
        candidates=[]
        if deadline is not None and bob>=deadline:candidates.append((deadline,'NEXT_TRADING_DAY_DEADLINE'))
        interval=self.blackout(position.cs,bob)
        if interval:candidates.append((interval[0],'PRE_LONG_CLOSURE_EXIT'))
        return min(candidates) if candidates else None
