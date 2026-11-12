"""Real supervisor/entry/consumer paths; synthetic inputs, no trading network."""
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timezone
import json,queue
import pandas as pd
import pytest
from futures_trend_quant.live import paper_clock_health as ch,paper_tq_market as tm
from futures_trend_quant.live.paper_five import FiveRunner
from futures_trend_quant.live.paper_tq import atomic,append,POLICY
from futures_trend_quant.live.paper_symbol_health import POLICY as ISOLATION
from test_paper_execution import engine,signal,NOW
from test_paper_runtime_health_v6 import setup_health,write


def old_proof():
    return dict(reason='SYSTEM_CLOCK_UNTRUSTED',detail='CLOCK_RUN_IDENTITY',
                diagnostic=dict(proof_run_id='OLD',expected_run_id='NEW'))


def runner(tmp_path,monkeypatch,age=1,alive=True):
    r=FiveRunner.__new__(FiveRunner);r.c=dict(clock_policy_version=ch.VERSION,clock_startup_policy=ch.STARTUP_POLICY)
    r.started=NOW-pd.Timedelta(seconds=125);r.clock_worker_started_monotonic=100.
    r.clock_worker=SimpleNamespace(is_alive=lambda:alive);r.clock_first_current_ready=False
    r.evidence=tmp_path;r.run_id='NEW';r.c['config_sha256']='C';r.events=[];r.notices=[];r.pauses=[]
    r.engine=SimpleNamespace(s=dict(pause_new=False),pause_entries=lambda *a:r.pauses.append(a))
    r.event=lambda *a,**k:r.events.append((a,k));r.notice=lambda *a,**k:r.notices.append((a,k))
    monkeypatch.setattr(ch.time,'monotonic',lambda:100.+age)
    return r


@pytest.mark.parametrize('age',[0,1,59.999])
def test_slow_host_init_does_not_consume_actual_clock_worker_grace(tmp_path,monkeypatch,age):
    r=runner(tmp_path,monkeypatch,age);monkeypatch.setattr(ch,'health',lambda c:old_proof())
    r.supervise_clock(NOW)
    assert not r.pauses and not r.notices and r.runtime_clock_health['startup_wait']
    assert r.clock_problem=='SYSTEM_CLOCK_UNTRUSTED'  # never reported healthy


@pytest.mark.parametrize('bad',['timeout','dead','current_bad','profile','future','permission'])
def test_real_clock_faults_never_receive_startup_grace(tmp_path,monkeypatch,bad):
    r=runner(tmp_path,monkeypatch,60 if bad=='timeout' else 1,bad!='dead');h=old_proof()
    if bad=='current_bad':h.update(detail='CLOCK_HEALTH_UNTRUSTED',diagnostic=dict(proof_run_id='NEW',expected_run_id='NEW'))
    if bad=='profile':h['diagnostic']['proof_run_id']='NEW'
    if bad=='future':h.update(detail='CLOCK_HEALTH_STALE',diagnostic=dict(proof_run_id='NEW',expected_run_id='NEW'))
    if bad=='permission':h.update(detail='JSON_READ_FAILED',diagnostic=dict(file='clock_health.local.json',category='PermissionError'))
    monkeypatch.setattr(ch,'health',lambda c:h);r.supervise_clock(NOW)
    assert len(r.pauses)==1 and len(r.notices)==1 and not r.runtime_clock_health['startup_wait']


def test_first_current_good_proof_permanently_ends_grace(tmp_path,monkeypatch):
    r=runner(tmp_path,monkeypatch);monkeypatch.setattr(ch,'health',lambda c:dict(reason=None,status='HEALTHY'))
    r.supervise_clock(NOW);assert r.clock_first_current_ready
    monkeypatch.setattr(ch,'health',lambda c:old_proof());r.supervise_clock(NOW)
    assert len(r.pauses)==1
    r.engine.s['pause_new']=True
    monkeypatch.setattr(ch,'health',lambda c:dict(reason=None,status='HEALTHY'));r.supervise_clock(NOW)
    assert r.engine.s['pause_new']  # recovery never consumes operator authority


def test_only_missing_clock_file_can_wait_not_missing_manifest():
    h=dict(reason='SYSTEM_CLOCK_UNTRUSTED',diagnostic=dict(category='FileNotFoundError',file='clock_health.local.json'))
    c=dict(clock_startup_policy=ch.STARTUP_POLICY)
    assert ch.startup_wait(c,h,1,True,False,2)
    h['diagnostic']['file']='run_manifest.json';assert not ch.startup_wait(c,h,1,True,False,2)


def test_real_entry_rejects_bootstrap_service_without_pause_or_wal_then_rechecks_clock(tmp_path):
    e,t=engine(tmp_path);row=setup_health(e.c);e.c['clock_startup_policy']=ch.STARTUP_POLICY
    ev=Path(e.c['evidence_dir']);service=json.loads((ev/'service_health.local.json').read_text())
    service['entry_gate_ready']=False;write(ev/'service_health.local.json',service)
    write(ev/'clock_health.local.json',dict(row,run_id='OLD'))
    assert e.open(signal(),NOW)==(False,'SERVICE_RECOVERY_PENDING')
    assert not e.s['pause_new'] and not e.s['intents'] and not t.jobs
    # Even an incorrectly ready service proof cannot bypass the final clock.
    service['entry_gate_ready']=True;write(ev/'service_health.local.json',service)
    assert e.open(signal(),NOW)==(False,'SYSTEM_CLOCK_UNTRUSTED')
    assert e.s['pause_new'] and not e.s['intents'] and not t.jobs


@pytest.mark.parametrize('symbol,cs',[('DCE.jd2612','DCE.JD'),('CZCE.CJ701','CZCE.CJ')])
@pytest.mark.parametrize('outcome',['recover','absent','stale','fatal','legacy'])
def test_actual_tq_consumer_waits_per_product_and_recovers_naturally(tmp_path,monkeypatch,symbol,cs,outcome):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,11,12,2,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(tm,'datetime',FixedDateTime)
    from futures_trend_quant.live import paper_runner
    spool=tmp_path/'spool';spool.mkdir();(spool/'health.jsonl').write_text('{}\n')
    atomic(tmp_path/'meta.json',dict(dates=['2026-11-11','2026-11-12','2026-11-13'],sessions=[dict(start='09:00',end='15:00')]))
    c=dict(root=str(tmp_path),csymbol=cs,config_sha256='C',tq_run_id='RUN',tq_spool=str(spool),
           state_dir=str(tmp_path/'state'),symbol_market_policy=ISOLATION,tq_input_wait_policy=tm.INPUT_WAIT_POLICY)
    if outcome=='legacy':c.pop('tq_input_wait_policy')
    w=SimpleNamespace(c=c,stop=False,bars=lambda *a:[]);events=[]
    def emit(e):
        events.append(e)
        if e[0]=='INITIAL_CACHE':w.stop=True
    w.events=SimpleNamespace(put=emit)
    monkeypatch.setattr(tm,'load_manifest',lambda *a:dict(products={cs:dict(symbol=symbol,metadata='meta.json',seed='seed.parquet')}))
    monkeypatch.setattr(tm.pd,'read_parquet',lambda *a:pd.DataFrame())
    monkeypatch.setattr(paper_runner,'warmup_coverage',lambda *a:dict(test_only=True))
    count=[0];mono=[0.];monkeypatch.setattr(tm.time,'monotonic',lambda:mono[0])
    proof=dict(policy=POLICY,config_sha256='C',run_id='RUN',status='RUNNING',product_status={cs:dict(ready=False,reason='TQ_NONFINITE_QUOTE')})
    def health(*a):
        return dict(proof,updated_at=(FixedDateTime.now(timezone.utc)-pd.Timedelta(seconds=4 if outcome=='stale' else 0)).isoformat(),
                    status='ERROR' if outcome=='fatal' else 'RUNNING')
    monkeypatch.setattr(tm,'latest_committed',health)
    def sleep(_):
        count[0]+=1;mono[0]+=100.
        if count[0]==3:
            if outcome!='recover':w.stop=True;return
            proof['product_status'][cs]['ready']=True
            stem=cs.replace('.','_');(spool/(stem+'.quotes.jsonl')).write_text('')
            now=FixedDateTime.now(timezone.utc);bob=pd.Timestamp(now).floor('min')-pd.Timedelta(minutes=1)
            payload=dict(policy=POLICY,config_sha256='C',run_id='RUN',symbol=symbol,updated_at=now.isoformat(),known_at=now.isoformat(),
                forming_bob_ns=bob.value+60_000_000_000,rows=[dict(datetime=bob.value,open=100,high=101,low=99,close=100,volume=5,close_oi=100)])
            append(spool/(stem+'.bars.jsonl'),payload)
    monkeypatch.setattr(tm.time,'sleep',sleep)
    if outcome=='legacy':
        with pytest.raises(TimeoutError,match='TQ_INITIAL_DATA_TIMEOUT'):tm.consume(w)
    elif outcome in ('stale','fatal'):
        with pytest.raises(ValueError,match='TQ_FEED_'):tm.consume(w)
    else:
        tm.consume(w)
        assert mono[0]>=300 and any(e[0]=='SOURCE_STATUS' and not e[1]['ready'] for e in events)
        assert any(e[0]=='INITIAL_CACHE' for e in events)==(outcome=='recover')
        assert not any(e[0] in ('QUOTE','NEW_BARS') for e in events)
