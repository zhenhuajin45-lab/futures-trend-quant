from pathlib import Path
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import hashlib,json
import pytest
from futures_trend_quant.live import paper_clock_health as ch,paper_tq as tq
from futures_trend_quant.live.paper_io import JsonReadError
from test_paper_runtime_health_v6 import setup_health,write
from test_paper_execution import engine


def test_retained_clock_sample_must_still_be_valid_when_collection_finishes():
    good=ch.collect('C','R',lambda s:dict(source=s,offset_seconds=.01,rtt_seconds=.01))
    good.update(sample_monotonic=100.,at='2026-11-12T00:00:00+00:00')
    loss=dict(good,status='UNTRUSTED',reason='CLOCK_SOURCE_QUORUM',sample_monotonic=185.,collection_completed_monotonic=195.,errors=[dict(retryable=True)])
    assert ch.retain_bounded_sample(good,loss)['status']=='UNTRUSTED'
    loss['collection_completed_monotonic']=189.
    retained=ch.retain_bounded_sample(good,loss)
    assert retained['sample_monotonic']==100. and retained['at']==good['at'] and retained['probe_degraded']


@pytest.mark.parametrize('age,expected',[(0,30),(60,15),(75,1),(89,1),(91,1)])
def test_reprobe_schedules_before_original_sample_expiry_without_extending_it(age,expected):
    r=dict(status='HEALTHY',sample_monotonic=100.)
    assert ch.next_probe_delay(r,100+age)==expected and r['sample_monotonic']==100.
    assert ch.next_probe_delay(dict(r,status='UNTRUSTED'),100+age)==5


def test_actual_clock_worker_journals_retention_and_does_not_renew_sample(tmp_path,monkeypatch):
    good=ch.collect('C','R',lambda s:dict(source=s,offset_seconds=.01,rtt_seconds=.01))
    good.update(sample_monotonic=1000.)
    loss=dict(good,status='UNTRUSTED',reason='CLOCK_SOURCE_QUORUM',sample_monotonic=1070.,collection_completed_monotonic=1078.,errors=[dict(retryable=True,error='TimeoutError')])
    values=iter([good,loss]);monkeypatch.setattr(ch,'collect',lambda *a:next(values));mono=iter([1000.,1078.]);monkeypatch.setattr(ch.time,'monotonic',lambda:next(mono))
    delays=[]
    class End(Exception):pass
    def sleep(delay):
        delays.append(delay)
        if len(delays)==2:raise End()
    monkeypatch.setattr(ch.time,'sleep',sleep)
    with pytest.raises(End):ch.clock_process(tmp_path,'C','R',ch.SAMPLER_POLICY)
    rows=[json.loads(x) for x in (tmp_path/'clock_samples.local.jsonl').read_text().splitlines()]
    assert len(rows)==2 and rows[-1]['retained'] and rows[-1]['measurement']['collection_completed_monotonic']==1078.
    published=json.loads((tmp_path/'clock_health.local.json').read_text())
    assert published['sample_monotonic']==1000. and published['sampler_sequence']==2
    assert delays==[30.,1.]


def test_structured_clock_io_failure_remains_a_guarded_rejection_not_keyerror(tmp_path,monkeypatch):
    from futures_trend_quant.live import paper_io
    e,_=engine(tmp_path);setup_health(e.c)
    error=JsonReadError(Path(e.c['evidence_dir'])/'clock_health.local.json','OPEN',PermissionError(13,'SYNTHETIC_PRIVATE_TEXT'),3)
    monkeypatch.setattr(paper_io,'read_json',lambda p:(_ for _ in ()).throw(error))
    result=ch.health(e.c)
    assert result['reason']=='SYSTEM_CLOCK_UNTRUSTED' and result['detail']==error.details['code']
    assert result['diagnostic']['stage']=='OPEN' and result['diagnostic']['attempts']==3
    with pytest.raises(ValueError,match='SYSTEM_CLOCK_UNTRUSTED'):ch.require_clock(e.c)
    assert 'SYNTHETIC_PRIVATE_TEXT' not in json.dumps(result)


@pytest.mark.parametrize('bad',['stale','future','step','bad_rtt','missing_rtt','wrong_sampler','nonfinite_age'])
def test_clock_failure_diagnostics_preserve_actual_age_and_original_native_guards(tmp_path,bad):
    e,_=engine(tmp_path);row=setup_health(e.c);mono=row['sample_monotonic']+1;wall=datetime.fromisoformat(row['at']).timestamp()+1
    if bad=='stale':mono+=90;wall+=90
    elif bad=='future':mono-=2;wall-=2
    elif bad=='step':wall+=1
    elif bad=='bad_rtt':row['samples'][0]['rtt_seconds']=1.01
    elif bad=='missing_rtt':row['samples'][0].pop('rtt_seconds')
    elif bad=='wrong_sampler':e.c['clock_sampler_policy']=ch.SAMPLER_POLICY
    else:row['sample_monotonic']=float('nan')
    write(Path(e.c['evidence_dir'])/'clock_health.local.json',row)
    result=ch.health(e.c,monotonic=mono,wall=wall)
    assert result['reason']=='SYSTEM_CLOCK_UNTRUSTED' and result['diagnostic']['proof_run_id']=='CURRENT'
    if bad=='stale':assert result['diagnostic']['age_seconds']==pytest.approx(91)
    json.dumps(result,allow_nan=False)


@pytest.mark.parametrize('bad',['stale','future','run','status','config'])
def test_tq_native_proof_rejection_is_bound_to_exact_committed_line(tmp_path,bad):
    now=datetime.now(timezone.utc);c=dict(evidence_dir='ev');row=dict(policy=tq.POLICY,config_sha256='C',run_id='GEN',status='RUNNING',updated_at=now.isoformat(),pid=123)
    if bad=='stale':row['updated_at']=(now-timedelta(seconds=4)).isoformat()
    elif bad=='future':row['updated_at']=(now+timedelta(seconds=1)).isoformat()
    elif bad=='run':row['run_id']='OLD'
    elif bad=='status':row['status']='ERROR'
    else:row['config_sha256']='OLD'
    tq.atomic(tmp_path/'ev/run_manifest.json',dict(run_id='GEN',config_sha256='C'))
    p=tmp_path/'ev/tq_feed/GEN/health.jsonl';tq.append(p,row);raw=p.read_bytes()
    with p.open('ab') as f:f.write(b'{"UNCOMMITTED":')
    with pytest.raises(tq.TqProofError) as ex:tq.current_feed_proof(tmp_path,c,'C',now)
    receipt=ex.value.details['receipt'];assert receipt['record_sha256']==hashlib.sha256(raw).hexdigest()
    assert receipt['record_offset']==0 and receipt['record_end']==len(raw) and receipt['file_bytes']>len(raw)
    assert ex.value.details['expected_run_id']=='GEN' and ex.value.details['proof_pid']==123


def test_stale_startup_bar_cannot_be_displayed_as_current_even_with_market_available(tmp_path):
    from futures_trend_quant.live.paper_symbol_health import POLICY,display_status,current_m15_event
    now=datetime(2026,11,12,9,31,tzinfo=timezone(timedelta(hours=8)))
    tq.atomic(tmp_path/'config/paper_observation_five.local.json',dict(symbol_market_policy=POLICY))
    result=display_status(tmp_path,'DCE.M',now,bar_eob='2026-11-11T23:00:00+08:00')
    assert not result['ready'] and result['reason']=='HISTORICAL_BAR_NOT_CURRENT_SIGNAL'
    s=dict(symbol_market_policy=POLICY,products={'DCE.M':dict(core={'last_closed_m15':'2026-11-11T23:00:00+08:00'})})
    assert not current_m15_event(s,'DCE.M','2026-11-11T23:00:00+08:00',now)
    s['products']['DCE.M']['core']['last_closed_m15']='2026-11-12T09:30:00+08:00'
    assert current_m15_event(s,'DCE.M','2026-11-12T01:30:00+00:00',now)


def test_tq_receipt_is_limited_to_size_captured_before_concurrent_append(monkeypatch):
    import io
    first=b'{"sequence":1}\n';second=b'{"sequence":2}\n'
    class Growing(io.BytesIO):
        def seek(self,offset,whence=0):
            if whence==2:self.captured=True
            elif getattr(self,'captured',False):
                self.captured=False;super().seek(0,2);self.write(second)
            return super().seek(offset,whence)
    monkeypatch.setattr(tq,'shared_open',lambda _:Growing(first))
    row,receipt=tq.latest_committed('health.jsonl',with_receipt=True)
    assert row['sequence']==1 and receipt['record_end']==receipt['file_bytes']==len(first)
    assert receipt['record_sha256']==hashlib.sha256(first).hexdigest()
