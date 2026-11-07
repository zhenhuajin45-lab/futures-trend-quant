"""Single-account SIM subprocess; blocking native calls never block the deadline timer."""
from pathlib import Path
import json
import queue
import subprocess
import threading
import time
import uuid
from datetime import datetime,timezone


class _SerialTransport:
    def __init__(self,config,config_path,role='WRITER'):
        self.role=role
        self.account=config['account_id'];self.jobs=queue.Queue();self.results=queue.Queue()
        self.current=None;self.started=None;self.last_reply=0.;self.closed=False
        root=Path(config['root']);logs=Path(config['evidence_dir']);logs.mkdir(parents=True,exist_ok=True)
        suffix='_reader' if role=='READ_ONLY' else ''
        self.trace_path=logs/('rpc_trace'+suffix+'.local.jsonl')
        self.log=(logs/('sim_sdk'+suffix+'.local.log')).open('a',encoding='utf8')
        self.process=subprocess.Popen([config['trade_python'],str(root/'futures_trend_quant/live/paper_sim_bridge.py'),str(config_path),role],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf8',bufsize=1,
            cwd=root,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.thread=threading.Thread(target=self._loop,name='paper-sim-rpc',daemon=True);self.thread.start()

    def request(self,op,args=None,rid=None):
        if self.closed:raise RuntimeError('closed RPC lane; no automatic resend')
        if getattr(self,'role','WRITER')=='READ_ONLY' and op!='snapshot':raise ValueError('read-only capability')
        # Coordinator does not queue periodic snapshots behind risk RPCs.
        # Replies remain busy until applied, closing the native-complete/state-stale race.
        if op=='snapshot' and self.busy():return None
        rid=rid or str(uuid.uuid4());job=dict(id=rid,op=op,args=args or {},account_id=self.account,
            queued_at=datetime.now(timezone.utc).isoformat())
        self.trace('QUEUED',job);self.jobs.put(job);return rid

    def trace(self,phase,job):
        if not getattr(self,'trace_path',None):return
        with self.trace_path.open('a',encoding='utf8') as f:
            f.write(json.dumps(dict(phase=phase,rpc_id=job['id'],op=job['op'],at=datetime.now(timezone.utc).isoformat()))+'\n')
            f.flush()

    def describe_busy(self):
        current=self.current
        with self.jobs.mutex:
            waiting=[dict(rpc_id=j['id'],op=j['op']) for j in self.jobs.queue]
        with self.results.mutex:
            completed=[dict(rpc_id=j['id'],op=j['op'],returned_at=r.get('local_received_at','UNKNOWN')) for j,r in self.results.queue]
        return dict(current=None if current is None else dict(rpc_id=current['id'],op=current['op'],
                    queued_at=current.get('queued_at','UNKNOWN'),sent_at=current.get('sent_at','UNKNOWN')),
                    waiting=waiting,unapplied_replies=len(completed),completed_replies=completed)

    def _loop(self):
        while not self.closed:
            job=self.jobs.get();self.current=job;self.started=time.monotonic()
            try:
                job['sent_at']=datetime.now(timezone.utc).isoformat()
                self.trace('SENT',job)
                self.process.stdin.write(json.dumps(job)+'\n');self.process.stdin.flush()
                while True:
                    line=self.process.stdout.readline()
                    if not line:raise RuntimeError('simulation worker disconnected; do not resend an uncertain submit')
                    try:reply=json.loads(line)
                    except ValueError:
                        self.log.write(line);self.log.flush();continue
                    if reply.get('id')==job['id']:break
                reply['local_received_at']=datetime.now(timezone.utc).isoformat()
                self.trace('RETURNED',job)
                self.results.put((job,reply));self.last_reply=time.monotonic()
            except Exception as exc:
                self.results.put((job,dict(ok=False,error_type=type(exc).__name__,reason='RPC_OUTCOME_UNKNOWN_NO_RETRY')))
                self.closed=True
            self.current=None;self.started=None

    def busy(self):return self.current is not None or not self.jobs.empty() or not self.results.empty()

    def hung(self):return self.closed or (self.started is not None and time.monotonic()-self.started>3)

    def drain(self):
        out=[]
        while True:
            try:out.append(self.results.get_nowait())
            except queue.Empty:return out


class SimTransport(_SerialTransport):
    """V5 routes periodic reads away from the single writer; legacy remains stable."""
    def __init__(self,config,config_path):
        self.split=config.get('execution_policy_version')=='PAPER_EXEC_V5'
        if not self.split:
            super().__init__(config,config_path);return
        self.writer=_SerialTransport(config,config_path,'WRITER')
        self.reader=_SerialTransport(config,config_path,'READ_ONLY')
        self.process=self.writer.process

    def request(self,op,args=None,rid=None):
        if not getattr(self,'split',False):return super().request(op,args,rid)
        return (self.reader if op=='snapshot' else self.writer).request(op,args,rid)

    def busy(self):
        return self.writer.busy() if getattr(self,'split',False) else super().busy()

    def hung(self):
        return self.writer.hung() if getattr(self,'split',False) else super().hung()

    def describe_busy(self):
        if not getattr(self,'split',False):return super().describe_busy()
        return dict(writer=self.writer.describe_busy(),reader=self.reader.describe_busy(),reader_hung=self.reader.hung())

    def drain(self):
        if not getattr(self,'split',False):return super().drain()
        # Apply writer receipts before read snapshots; newer minimal exit truth wins.
        return self.writer.drain()+self.reader.drain()

    def shutdown(self):
        for lane in ([self.writer,self.reader] if getattr(self,'split',False) else [self]):
            lane.closed=True
            if lane.process.poll() is None:
                lane.process.terminate();lane.process.wait(timeout=3)
