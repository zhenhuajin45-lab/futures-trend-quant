"""Closed-bar causal lifecycle. All state is serializable, including tombstones."""
from dataclasses import dataclass, field, asdict
from copy import deepcopy
from datetime import datetime
from futures_trend_quant.core.entry.models import EvidenceEvent


@dataclass
class EpisodeBook:
    active: dict | None = None
    armed: bool = False
    previous_direction: str | None = None
    symbol: str | None = None
    last_eob: str | None = None
    seen_patterns: set = field(default_factory=set)
    ended: list = field(default_factory=list)
    history: list = field(default_factory=list)

    def to_dict(self):
        d=asdict(self);d['seen_patterns']=sorted(self.seen_patterns);return d

    @classmethod
    def from_dict(cls,d):
        d=deepcopy(d);d['seen_patterns']=set(d['seen_patterns']);return cls(**d)

    def end(self,reason,at):
        if self.active:
            self.ended.append(dict(**self.active,end_reason=reason,ended_at=at))
        self.active=None;self.armed=False

    def step(self,bar,*,direction,ema,atr,pattern=None,zone_atr=.5,max_bars=6):
        at=bar.eob.isoformat()
        if self.last_eob and at<=self.last_eob:raise ValueError('closed M5 must strictly advance')
        gap=self.last_eob and (bar.eob-datetime.fromisoformat(self.last_eob)).total_seconds()>300
        if gap or bar.symbol!=self.symbol or direction!=self.previous_direction:
            self.end('SESSION_OR_AUTHORIZATION_CHANGE',at)
        self.last_eob=at;self.symbol=bar.symbol;self.previous_direction=direction
        if direction not in ('bull','bear') or ema is None or atr is None or atr<=0:
            self.end('NO_AUTHORIZATION_OR_INDICATORS',at);return
        sign=1 if direction=='bull' else -1
        outside=sign*(bar.close-ema)>zone_atr*atr
        near=abs(bar.close-ema)<=zone_atr*atr
        fresh_pattern=pattern is not None and pattern.eob==bar.eob and pattern.bar_id not in self.seen_patterns
        if fresh_pattern:self.seen_patterns.add(pattern.bar_id)
        active=self.active
        if active:
            active['age']+=1
            broken=bar.close<active['low'] if direction=='bull' else bar.close>active['high']
            if broken or active['age']>=max_bars:
                self.end('START_CLOSE_BROKEN' if broken else 'EPISODE_EXPIRED',at)
            elif fresh_pattern and active['confirmation'] is None:
                active['confirmation']=dict(bar_id=pattern.bar_id,eob=at)
                active['evidence_id']=active['id']+'|CONFIRM|'+pattern.bar_id
        elif self.armed and near:
            self.active=dict(id=bar.symbol+'|'+direction+'|'+bar.identity,start=at,
                             low=bar.low,high=bar.high,age=0,confirmation=None,evidence_id=None)
            self.armed=False  # same-bar confirmation deliberately ignored
        elif outside:self.armed=True
        self.history.append(dict(at=at,direction=direction,near=near,outside=outside,
                                 pattern=None if pattern is None else pattern.bar_id,
                                 active=None if self.active is None else dict(self.active)))

    def evidence(self,sequence):
        a=self.active
        if a is None or a['confirmation'] is None:return None
        # Retain the confirmation as trigger: later touches cannot renew it.
        c=a['confirmation'];return EvidenceEvent('ordered_b_confirmation',self.previous_direction,
            a['evidence_id'],sequence,datetime.fromisoformat(c['eob']))

    def consume(self,evidence_id,at):
        if not evidence_id or self.active is None or self.active['confirmation'] is None or self.active['evidence_id']!=evidence_id:
            raise ValueError('cannot consume unknown or ended episode')
        if datetime.fromisoformat(at)<datetime.fromisoformat(self.last_eob):
            raise ValueError('consumption cannot rewrite an earlier prefix')
        self.end('CONSUMED_BY_CONFIRMED_FILL',at)
