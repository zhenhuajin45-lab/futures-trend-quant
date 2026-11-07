"""PAPER_ENTRY_V5 closed-M5 retracement lifecycle; no SDK or research dependency."""
from dataclasses import dataclass,field,asdict
from datetime import datetime
from copy import deepcopy
from ..facts.indicators import calculate_indicators
from .evidence import _latest_near_extreme_pattern
from .models import PathCandidate


@dataclass
class OrderedBBook:
    active: dict | None = None
    armed: bool = False
    direction: str = 'none'
    symbol: str | None = None
    session_id: str | None = None
    last_eob: str | None = None
    patterns: set = field(default_factory=set)
    tombstones: list = field(default_factory=list)

    def to_dict(self):
        d=asdict(self);d['patterns']=sorted(self.patterns);return d

    @classmethod
    def from_dict(cls,raw):
        d=deepcopy(raw);d['patterns']=set(d.get('patterns',[]));return cls(**d)

    def end(self,reason,at):
        if self.active:self.tombstones.append(dict(**self.active,ended_at=at,reason=reason))
        self.active=None;self.armed=False

    def advance(self,bars,direction,sessions,rules,after):
        for i,bar in enumerate(bars):
            at=bar.eob.isoformat()
            if self.last_eob and bar.eob<=datetime.fromisoformat(self.last_eob):continue
            if self.last_eob is None and after and bar.eob<=datetime.fromisoformat(after):continue
            session=sessions.get(bar.identity)
            if not session:
                self.end('SESSION_ID_UNKNOWN',at);self.last_eob=at;continue
            if bar.symbol!=self.symbol or direction!=self.direction or session!=self.session_id:
                self.end('CONTRACT_AUTHORIZATION_OR_SESSION_CHANGED',at)
            elif self.last_eob and (bar.eob-datetime.fromisoformat(self.last_eob)).total_seconds()!=300:
                self.end('MISSING_M5_INSIDE_SESSION',at)
            self.direction=direction;self.symbol=bar.symbol;self.session_id=session;self.last_eob=at
            if direction not in ('bull','bear'):self.end('NO_DIRECTION_AUTHORIZATION',at);continue
            prefix=list(bars[max(0,i+1-1200):i+1]);ind=calculate_indicators(prefix)
            ema,atr=ind.ema34[-1],ind.atr14[-1]
            if ema is None or atr is None or atr<=0:self.end('M5_INDICATORS_UNKNOWN',at);continue
            pattern=_latest_near_extreme_pattern(prefix,ind,direction,int(rules['m5_flat_lookback']),float(rules['evidence_position_atr']))
            fresh=bool(pattern and pattern.eob==bar.eob and pattern.bar_id not in self.patterns)
            if fresh:self.patterns.add(pattern.bar_id)
            sign=1 if direction=='bull' else -1;zone=float(rules['pullback_min_atr'])*atr
            outside=sign*(bar.close-ema)>zone;near=abs(bar.close-ema)<=zone
            if self.active:
                a=self.active;a['age']+=1
                broken=bar.close<a['low'] if direction=='bull' else bar.close>a['high']
                if broken or a['age']>=int(rules['m5_flat_lookback']):
                    self.end('START_CLOSE_BROKEN' if broken else 'EPISODE_EXPIRED',at)
                elif fresh and a['confirmation_bar_id'] is None:
                    a.update(confirmation_bar_id=bar.identity,confirmation_eob=at)
            elif self.armed and near:
                self.active=dict(process_id=bar.symbol+'|'+direction+'|'+bar.identity,start_bar_id=bar.identity,start_eob=at,
                    low=bar.low,high=bar.high,age=0,confirmation_bar_id=None,confirmation_eob=None)
                self.armed=False
            elif outside:self.armed=True

    def candidate(self,bars):
        a=self.active
        if not a or not a['confirmation_bar_id']:return None
        i=next((i for i,b in enumerate(bars) if b.identity==a['confirmation_bar_id']),None)
        if i is None:return None
        return PathCandidate('B',self.direction,a['confirmation_bar_id'],i,datetime.fromisoformat(a['confirmation_eob']),a['process_id'])

    def consume(self,evidence_id,at):
        a=self.active
        if a and a['confirmation_bar_id'] and evidence_id==f"B|{self.direction}|{a['process_id']}|{a['confirmation_bar_id']}":
            self.end('CONSUMED_BY_CONFIRMED_FILL',at)
