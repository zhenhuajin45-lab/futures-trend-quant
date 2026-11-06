"""Sequence/restart contract for the S0 explanation layer; no order imports."""
from copy import deepcopy
import hashlib
import json
from futures_trend_quant.live.trend_observation import build,clock,VERSION


class FuturesTrendObserver:
    def __init__(self,symbol):
        self.symbol=symbol;self.last_eob=None;self.records={}

    def to_dict(self):
        return dict(schema=VERSION,symbol=self.symbol,last_eob=self.last_eob,records=deepcopy(self.records))

    @classmethod
    def from_dict(cls,raw):
        if raw['schema']!=VERSION:raise ValueError('observation schema mismatch')
        obj=cls(raw['symbol']);obj.last_eob=raw['last_eob'];obj.records=deepcopy(raw['records']);return obj

    def push(self,facts,*,asof,expected_previous_eob,**context):
        f=deepcopy(facts);eob=f['eob']
        if f['symbol']!=self.symbol or f['bar_id'].split('|')[:2]!=[self.symbol,'900s'] or f['bar_id'].rsplit('|',1)[-1]!=eob:
            raise ValueError('contract/frequency/bar identity mismatch')
        if clock(eob)>asof:raise ValueError('future unclosed bar')
        input_context={k:v for k,v in context.items() if k!='computed_at'}
        blob=json.dumps(dict(facts=f,context=input_context),sort_keys=True,default=str).encode();digest=hashlib.sha256(blob).hexdigest()
        if eob in self.records:
            if self.records[eob]['input_sha256']!=digest:raise ValueError('closed facts revised')
            return deepcopy(self.records[eob]['observation'])
        if self.last_eob and (clock(eob)<=clock(self.last_eob) or expected_previous_eob!=self.last_eob):
            raise ValueError('out of order or independent-calendar gap')
        observation=build(f,**context)
        # Incomplete records remain visible but never advance the closed prefix.
        if context.get('closed') is True and context.get('complete') is True:
            self.last_eob=eob;self.records[eob]=dict(input_sha256=digest,observation=deepcopy(observation))
        return observation
