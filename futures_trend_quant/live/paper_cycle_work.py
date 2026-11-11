"""Bound non-execution queue work; never change signal or risk deadlines."""
import time
from datetime import datetime,timezone

POLICY='ACCOUNT_CYCLE_WORK_V1_20261111'


class CycleWork:
    def __init__(self,clock=None):
        self.clock=clock or time.monotonic;self.started=self.clock()
        self.at=datetime.now(timezone.utc).isoformat();self.phases={};self.queues={}

    def take(self,phase,lane,seconds=.05,maximum=64,per_lane=8):
        q=self.queues.setdefault(phase,dict(started=self.clock(),allowed_reads=0,lanes={},deferred=False))
        count=q['lanes'].get(lane,0)
        if self.clock()-q['started']>=seconds or q['allowed_reads']>=maximum or count>=per_lane:
            q['deferred']=True;return False
        q['allowed_reads']+=1;q['lanes'][lane]=count+1;return True

    def run(self,name,callback):
        began=self.clock()
        try:return callback()
        finally:
            d=self.phases.setdefault(name,dict(calls=0,total_seconds=0.,max_seconds=0.))
            elapsed=self.clock()-began;d['calls']+=1;d['total_seconds']+=elapsed;d['max_seconds']=max(d['max_seconds'],elapsed)

    def snapshot(self):
        return dict(policy=POLICY,started_at=self.at,duration_seconds=self.clock()-self.started,
                    phases=self.phases,queues=self.queues,
                    note='Allowed queue reads are work permits, not completed work or a liveness proof')


def products(r,phase):
    items=list(r.products.items())
    if not getattr(r,'_cycle_work',None) or not items:return items
    key='_queue_cursor_'+phase;start=getattr(r,key,0)%len(items);setattr(r,key,start+1)
    return items[start:]+items[:start]


def take(r,phase,lane,**limits):
    work=getattr(r,'_cycle_work',None)
    return work is None or work.take(phase,lane,**limits)
