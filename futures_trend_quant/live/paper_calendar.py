"""DCE calendar/session adapter for the existing V9 holding policy."""
from datetime import datetime
from types import SimpleNamespace
import numpy as np
import pandas as pd
from tools.short_swing_v9.policy import HoldingCalendar


class PaperCalendar:
    def __init__(self,dates,sessions,csymbol='DCE.M'):
        self.dates=sorted({str(x)[:10] for x in dates});self.cs=csymbol
        if len(self.dates)<3:raise ValueError('exchange calendar unavailable')
        self.clock=[];self.days=[]
        for i,day in enumerate(self.dates):
            for section in sessions:
                start=section['start'];end=section['end'];night=int(start.split(':')[0])>=20
                if night:
                    if not i:continue
                    previous=self.dates[i-1]
                    # Normal overnight or Friday night; no night immediately before an exchange long holiday.
                    if (pd.Timestamp(day)-pd.Timestamp(previous)).days not in (1,3):continue
                    base=previous
                else:base=day
                a=pd.Timestamp(base+' '+start,tz='Asia/Shanghai');b=pd.Timestamp(base+' '+end,tz='Asia/Shanghai')
                if b<=a:b+=pd.Timedelta(days=1)
                for t in pd.date_range(a+pd.Timedelta(minutes=1),b,freq='min'):
                    self.clock.append(t);self.days.append(day)
        order=np.argsort(pd.DatetimeIndex(self.clock).asi8)
        self.clock=pd.DatetimeIndex(self.clock)[order];self.days=np.array(self.days)[order]
        self.ns=self.clock.as_unit('ns').asi8
        if self.clock.duplicated().any():raise ValueError('duplicate expected minute')
        unique,first,counts=np.unique(self.days,return_index=True,return_counts=True)
        self.daily_counts={str(d):int(n) for d,n in zip(unique,counts)}
        self.first_open_ns={str(d):int(self.ns[i]-60_000_000_000) for d,i in zip(unique,first)}
        self.close_ns={str(d):int(self.ns[i+n-1]) for d,i,n in zip(unique,first,counts)}
        self.policy=HoldingCalendar({self.cs:dict(ns=self.ns,days=self.days)},buffer_minutes=5,weekend_mode='DAY_CLOSE')

    def day(self,now):
        t=pd.Timestamp(now)
        if t.tzinfo is None:raise ValueError('unknown decision timezone')
        local=t.tz_convert('Asia/Shanghai')
        if local.hour<20 and local.date().isoformat() in self.dates:return local.date().isoformat()
        i=int(np.searchsorted(self.ns,t.value,side='left'))
        if i>=len(self.ns):raise ValueError('calendar must be extended before new entries')
        return str(self.days[i])

    def market_open(self,now):
        t=pd.Timestamp(now);i=int(np.searchsorted(self.ns,t.value,side='right'))
        return bool(i<len(self.ns) and self.ns[i]-60_000_000_000<=t.value<self.ns[i])

    def entry_block(self,now):
        if not self.market_open(now):return 'OUTSIDE_CONTINUOUS_SESSION'
        return self.policy.entry_block(self.cs,self.day(now),pd.Timestamp(now).value)

    def due(self,entry_day,now):
        if entry_day not in self.policy.products[self.cs]['deadlines']:raise ValueError('unknown position trading day')
        return self.policy.due(SimpleNamespace(cs=self.cs,entry_day=entry_day),pd.Timestamp(now).value)

    def deadline(self,entry_day):
        deadline=self.policy.products[self.cs]['deadlines'].get(entry_day)
        if deadline is None:raise ValueError('next trading day unknown')
        return pd.Timestamp(deadline,tz='UTC').tz_convert('Asia/Shanghai').isoformat()


class PortfolioCalendar:
    """One account clock, per-contract execution/holding clocks."""
    def __init__(self):self.contracts={}
    def register(self,symbol,calendar):self.contracts[symbol]=calendar
    def for_symbol(self,symbol):
        if symbol not in self.contracts:raise ValueError('contract calendar not ready')
        return self.contracts[symbol]
    def day(self,now):
        if not self.contracts:raise ValueError('account calendar not ready')
        reference=next((c for c in self.contracts.values() if c.cs=='DCE.M'),next(iter(self.contracts.values())))
        return reference.day(now)
    def market_open(self,now):return any(c.market_open(now) for c in self.contracts.values())
