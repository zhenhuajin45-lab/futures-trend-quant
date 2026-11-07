from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import json
import pytest
from tools.ordered_b.episode import EpisodeBook
from futures_trend_quant.core.entry.models import EvidenceEvent

T=datetime(2026,11,6,21,tzinfo=timezone(timedelta(hours=8)))


def bar(n,close=100,symbol='DCE.m2701'):
    return SimpleNamespace(symbol=symbol,identity=f'{symbol}|300s|{(T+timedelta(minutes=n*5)).isoformat()}',
        eob=T+timedelta(minutes=n*5),close=close,low=close-1,high=close+1)


def step(book,n,close=100,pattern=False,direction='bull'):
    b=bar(n,close if direction=='bull' else 200-close)
    p=EvidenceEvent('pattern',direction,b.identity,n,b.eob) if pattern else None
    book.step(b,direction=direction,ema=100,atr=2,pattern=p)


@pytest.mark.parametrize('direction',['bull','bear'])
def test_early_touch_preserved_and_same_pattern_not_new_start(direction):
    b=EpisodeBook();step(b,0,104,direction=direction);step(b,1,100,direction=direction)
    start=b.active['id'];step(b,2,100,pattern=True,direction=direction);eid=b.active['evidence_id']
    step(b,3,100,direction=direction)
    assert b.active['id']==start and b.active['evidence_id']==eid
    assert b.evidence(10).eob==bar(2).eob


def test_prior_pattern_or_same_bar_cannot_confirm():
    b=EpisodeBook();step(b,0,104,pattern=True);step(b,1,100,pattern=True)
    assert b.active and b.active['confirmation'] is None
    step(b,2,100);assert b.evidence(2) is None


def test_close_break_tombstone_and_no_resurrection():
    b=EpisodeBook();step(b,0,104);step(b,1);step(b,2,100,pattern=True);step(b,3,98)
    assert b.active is None and b.ended[-1]['end_reason']=='START_CLOSE_BROKEN'
    step(b,4,100);assert b.active is None
    restored=EpisodeBook.from_dict(json.loads(json.dumps(b.to_dict())))
    step(restored,5,104);step(restored,6,100)
    assert restored.active['confirmation'] is None and len(restored.seen_patterns)==1


def test_restart_prefix_and_future_invariance():
    a=EpisodeBook()
    for n,c,p in [(0,104,False),(1,100,False),(2,100,True)]:step(a,n,c,p)
    saved=json.loads(json.dumps(a.to_dict()));b=EpisodeBook.from_dict(saved)
    step(a,3);step(b,3);assert a.to_dict()==b.to_dict()
    assert saved['history'][-1]['at']==bar(2).eob.isoformat()
    step(a,4,98);assert saved['active']['confirmation']['eob']==bar(2).eob.isoformat()
    with pytest.raises(ValueError):step(b,3)


def test_lifetime_and_session_or_direction_change():
    b=EpisodeBook();step(b,0,104);step(b,1)
    for n in range(2,8):step(b,n)
    assert b.active is None and b.ended[-1]['end_reason']=='EPISODE_EXPIRED'
    c=EpisodeBook();step(c,0,104);step(c,1);step(c,10)
    assert c.active is None
    step(c,11,104);step(c,12);step(c,13,direction='bear');assert c.active is None


def test_consumption_persists_and_cannot_be_repeated():
    b=EpisodeBook();step(b,0,104);step(b,1)
    with pytest.raises(ValueError):b.consume(None,bar(1).eob.isoformat())
    step(b,2,pattern=True)
    with pytest.raises(ValueError):b.consume(b.active['evidence_id'],bar(1).eob.isoformat())
    eid=b.active['evidence_id'];b.consume(eid,bar(2).eob.isoformat())
    restored=EpisodeBook.from_dict(b.to_dict());assert restored.active is None
    assert restored.ended[-1]['end_reason']=='CONSUMED_BY_CONFIRMED_FILL'
    with pytest.raises(ValueError):restored.consume(eid,bar(3).eob.isoformat())


def test_soft_deadline_does_not_release_real_transport_thread(tmp_path):
    import queue,threading,time
    from futures_trend_quant.live.paper_transport import SimTransport
    sent=threading.Event();release=threading.Event()
    class Input:
        def write(self,s):sent.set()
        def flush(self):pass
    class Output:
        def readline(self):
            assert release.wait(5)
            return json.dumps(dict(id='READ',ok=True,data={}))+'\n'
    t=SimTransport.__new__(SimTransport);t.account='SYNTHETIC';t.jobs=queue.Queue();t.results=queue.Queue()
    t.current=None;t.started=None;t.closed=False;t.last_reply=0.;t.trace_path=None
    t.log=(tmp_path/'fake.log').open('w');t.process=SimpleNamespace(stdin=Input(),stdout=Output())
    worker=threading.Thread(target=t._loop,daemon=True);worker.start()
    t.request('snapshot',rid='READ');assert sent.wait(2)
    t.started=time.monotonic()-4  # caller's 3-second soft threshold, native reader still blocked
    assert t.hung() and t.busy() and t.request('snapshot') is None
    assert t.describe_busy()['current']['rpc_id']=='READ'
    release.set()
    deadline=time.monotonic()+2
    while t.results.empty() and time.monotonic()<deadline:time.sleep(.001)
    assert t.busy()  # completed but unapplied reply also occupies the channel
    result=t.drain();assert len(result)==1 and result[0][0]['id']=='READ'
    assert not t.busy()
    t.closed=True;t.log.close()


@pytest.mark.parametrize('op',['snapshot','submit','cancel'])
def test_timeout_stop_request_and_actual_exit_are_distinct(op):
    from tools.ordered_b.rpc_lease import RpcLease
    g=RpcLease();g.send('R',op,0);g.exit_due_at=1;g.timeout(3);g.stop_requested(5)
    assert g.current and not g.recover(fresh_truth=True)
    assert g.exposure(72)==71
    g.child_exit_proven(72)
    assert not g.recover(fresh_truth=True)
    assert not g.recover(fresh_truth=True,manual_writer_handoff=True) if op!='snapshot' else True
    assert g.recover(fresh_truth=True,resolved_ids=['R'],manual_writer_handoff=True)
    g.reply('R',73)
    assert g.timeline[-1]['stage']=='LATE_OR_FOREIGN_REPLY_QUARANTINED'


def test_owned_read_worker_termination_proves_process_exit_not_order_completion():
    import subprocess,sys
    from tools.ordered_b.rpc_lease import RpcLease
    from tools.ordered_b.read_worker_supervisor import stop_owned_read_worker
    p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        lease=RpcLease();lease.send('READ','snapshot',0);lease.timeout(3)
        with pytest.raises(ValueError):stop_owned_read_worker(p,owned_pid=p.pid,role='WRITER',lease=lease,at=5)
        proof=stop_owned_read_worker(p,owned_pid=p.pid,role='READ_ONLY',lease=lease,at=5)
        assert p.poll() is not None and proof['local_worker_dead'] and not proof['writer_restart_allowed']
        assert not lease.recover(fresh_truth=True)
    finally:
        if p.poll() is None:p.kill();p.wait()
