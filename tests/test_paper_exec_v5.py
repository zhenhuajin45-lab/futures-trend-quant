"""Formal V5 execution paths with fake native API, no real orders."""
from datetime import timedelta
from futures_trend_quant.live.paper_transport import SimTransport
from test_paper_execution import engine,signal,ack,position,snapshot,quote,NOW,FakeTransport


def split():
    t=SimTransport.__new__(SimTransport);t.split=True;t.writer=FakeTransport();t.reader=FakeTransport()
    t.writer.describe_busy=lambda:{'fake':'writer'};t.reader.describe_busy=lambda:{'fake':'reader'}
    return t


def test_blocked_periodic_reader_does_not_own_writer_or_risk_timer(tmp_path):
    e,old=engine(tmp_path);assert e.open(signal(),NOW)[0];opening=ack(e,status=3,filled=1)
    e.apply_snapshot(snapshot(orders=[opening],positions=[position()]),NOW)
    t=split();t.reader.is_busy=True;e.transport=t;e.c['execution_policy_version']='PAPER_EXEC_V5'
    now=NOW+timedelta(seconds=72);e.quote('DCE.m2701',quote(now),now)
    e.force_exit('DCE.m2701','STOP_M1_TRIGGER',now);e.tick(now)
    assert t.writer.jobs[-1]['op']=='exit_snapshot' and e.s['due_events']
    e.accept(dict(op='exit_snapshot',id='EXIT_TRUTH',sent_at=str(now)),dict(ok=True,data=dict(positions=[position()],active=[])),now)
    e.tick(now)
    assert t.writer.jobs[-1]['op']=='submit' and t.writer.jobs[-1]['args']['position_effect']!=1
    assert e.positions()[0]['volume']==1 and not next(iter(e.s['due_events'].values()))['completed']
    assert t.reader.is_busy


def test_stale_read_result_never_refreshes_reconciliation(tmp_path):
    e,t=engine(tmp_path);e.c['execution_policy_version']='PAPER_EXEC_V5';before=e.last_snapshot
    e.accept(dict(op='snapshot',id='SLOW',sent_at=str(NOW)),dict(ok=True,data=snapshot()),NOW+timedelta(seconds=72))
    assert e.last_snapshot==before and not e.ready and e.block=='STALE_FULL_SNAPSHOT'


def test_unknown_cancel_is_not_automatically_resent(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);opening=ack(e,status=1,filled=0)
    e.accept(dict(op='cancel',id='C',args=dict(cl_ord_id=opening['cl_ord_id'])),dict(ok=False,native_cancel_invoked=True,reason='UNKNOWN'),NOW)
    intent=next(iter(e.s['intents'].values()));assert intent['cancel_outcome_unknown']
    n=len(t.jobs);e.cancel(intent,'STOP',NOW+timedelta(seconds=20));assert len(t.jobs)==n


def test_read_only_bridge_rejects_write_capabilities(tmp_path,monkeypatch):
    from test_paper_bridge import invoke,req,args
    from futures_trend_quant.live import paper_sim_bridge as b
    original=b.serve;monkeypatch.setattr(b,'serve',lambda path:original(path,'READ_ONLY'))
    result,calls=invoke(tmp_path,monkeypatch,[req('submit','S',arguments=args()),req('cancel','C',arguments=dict(cl_ord_id='O'))])
    assert all(not r['ok'] and r['reason']=='READ_ONLY_CAPABILITY_DENIED' for r in result)
    assert not any(name in ('submit','cancel') for name,_ in calls)
