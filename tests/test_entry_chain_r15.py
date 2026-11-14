"""Production-shaped regressions for bounded clock probes and identical bars."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
import time
import pytest
from futures_trend_quant.live.paper_clock_probe import BoundedClockCollector, POLICY
from futures_trend_quant.live.paper_clock_health import SOURCES,retain_bounded_sample,MAX_AGE
from futures_trend_quant.live.paper_aggregation import CompleteBucketCache
from futures_trend_quant.live.paper_data import complete_bars
from futures_trend_quant.core.models import Bar


def sample(source,offset=.01):
    return dict(source=source,offset_seconds=offset,rtt_seconds=.01,
                client_sent_monotonic=time.monotonic(),stratum=2)


def test_parallel_sources_start_together_and_publish_same_cycle():
    barrier=threading.Barrier(2)
    def probe(source):
        barrier.wait(timeout=1)
        return sample(source)
    c=BoundedClockCollector(probe,budget_seconds=.8)
    row=c.collect('C','R')
    assert row['status']=='HEALTHY' and row['probe_policy']==POLICY
    assert {s['source'] for s in row['samples']}==set(SOURCES)


def test_blocked_dns_has_bounded_collection_one_worker_and_discards_late_sample():
    release=threading.Event();entered=threading.Event();calls=[]
    def probe(source):
        calls.append(source)
        if source==SOURCES[0] and not release.is_set():
            entered.set();release.wait(2)
        return sample(source)
    c=BoundedClockCollector(probe,budget_seconds=.06)
    try:
        first=c.collect('C','R');assert entered.is_set()
        assert first['reason']=='CLOCK_SOURCE_QUORUM' and first['collection_seconds']<.4
        second=c.collect('C','R')
        assert second['reason']=='CLOCK_SOURCE_QUORUM'
        assert calls.count(SOURCES[0])==1  # No unbounded replacement DNS threads.
        release.set()
        third=c.collect('C','R')
        assert third['status']=='HEALTHY' and third['probe_cycle']==3
        assert calls.count(SOURCES[0])==2  # Fresh request, not late cycle-1 reply.
    finally:release.set()


@pytest.mark.parametrize('offset',[.50001,-.50001,1.])
def test_parallel_collection_does_not_relax_single_source_offset(offset):
    c=BoundedClockCollector(lambda s:sample(s,offset if s==SOURCES[0] else .01))
    assert c.collect('C','R')['reason']=='CLOCK_OFFSET'


def test_parallel_collection_rejects_disagreement_and_does_not_extend_old_sample():
    c=BoundedClockCollector(lambda s:sample(s,.2 if s==SOURCES[0] else -.2))
    assert c.collect('C','R')['reason']=='CLOCK_SOURCE_DISAGREEMENT'
    good=BoundedClockCollector(sample).collect('C','R')
    bad=dict(good,reason='CLOCK_SOURCE_QUORUM',status='UNTRUSTED',samples=[],errors=[dict(retryable=True)])
    retained=retain_bounded_sample(good,bad)
    assert retained['sample_monotonic']==good['sample_monotonic']
    bad['collection_completed_monotonic']=good['sample_monotonic']+MAX_AGE+.001
    assert retain_bounded_sample(good,bad)['status']=='UNTRUSTED'


def test_native_health_rejects_legacy_collector_when_new_policy_is_required(tmp_path):
    import json
    from test_paper_runtime_health_v6 import setup_health
    from futures_trend_quant.live import paper_clock_health as ch
    from test_paper_execution import NOW,configuration
    config=configuration(tmp_path)
    setup_health(config,at=NOW.isoformat(),sample_monotonic=1000.)
    config['clock_probe_policy']=POLICY
    result=ch.health(config,monotonic=1000.,wall=NOW.timestamp())
    assert result['reason']=='SYSTEM_CLOCK_UNTRUSTED'
    assert result['detail']=='CLOCK_PROBE_IDENTITY'


@pytest.mark.parametrize('symbol',['DCE.m2701','SHFE.au2612','CZCE.OI701','DCE.y2701','DCE.p2701',
                                    'CZCE.MA701','DCE.eg2701','DCE.jd2612','CZCE.CJ701','SHFE.ag2702'])
@pytest.mark.parametrize('mirror',[1,-1])
def test_incremental_aggregation_matches_original_across_weekend_gap_roll_and_restart(symbol,mirror):
    t=datetime(2026,11,13,20,59,tzinfo=timezone(timedelta(hours=8)))
    values=[]
    for n in range(95):
        e=t+timedelta(minutes=n+1)+(timedelta(days=3) if n>=60 else timedelta())
        p=100+mirror*n*.1
        values.append(Bar(symbol,'60s',e-timedelta(minutes=1),e,p,p+1,p-1,p,1,2,3))
    c=CompleteBucketCache()
    for i in range(1,len(values)+1):
        current=values[max(0,i-46):i]
        if i==65:c=CompleteBucketCache()  # Same exact window after restart.
        got=c.update(current)
        assert got==(complete_bars(current,5),complete_bars(current,15))
    hole=values[-40:-5]+values[-4:]
    assert c.update(hole)==(complete_bars(hole,5),complete_bars(hole,15))


def test_aggregation_cache_rejects_revised_and_duplicate_used_minute():
    t=datetime(2026,11,13,21,1,tzinfo=timezone.utc)
    b=Bar('DCE.m2701','60s',t-timedelta(minutes=1),t,100,101,99,100,1)
    c=CompleteBucketCache();c.update([b])
    with pytest.raises(ValueError,match='REVISED'):c.update([replace(b,close=100.5)])
    with pytest.raises(ValueError,match='DUPLICATE'):c.update([b,b])


def test_actual_signal_worker_consumes_cached_buckets_and_emits_original_input_clock(tmp_path):
    from futures_trend_quant.live.paper_data import SignalWorker,CORE_MIN_M15
    from futures_trend_quant.live.paper_aggregation import POLICY as AGG
    from futures_trend_quant.config import load_config,default_config_path
    from queue import Queue
    base=load_config(default_config_path())
    config=dict(root=str(tmp_path),state_dir=str(tmp_path),csymbol='DCE.M',
                base_config=base,warmup_m15=CORE_MIN_M15,core_aggregation_policy=AGG)
    w=SignalWorker(config,child=True);events=[]
    class Output:
        def put(self,event):
            events.append(event)
            if event[0] in ('CORE_READY','CORE_FAILED','WARMUP_FAILED'):w.stop=True
    w.outbox=Output()
    start=datetime(2026,11,5,0,0,tzinfo=timezone(timedelta(hours=8)))
    bars=[]
    for i in range(CORE_MIN_M15*15):
        end=start+timedelta(minutes=i+1)
        bars.append(Bar('DCE.m2701','60s',end-timedelta(minutes=1),end,100,101,99,100,1))
    received=(bars[-1].eob+timedelta(seconds=1)).isoformat()
    w.inbox.put(('NEW_BARS',bars,dict(source='GM_HOST_ON_BAR_CLOSED_M1',returned_at=received)))
    w._run()
    assert not [e for e in events if e[0].endswith('FAILED')]
    result=next(e[1] for e in events if e[0]=='CORE_RESULT')
    clock=result.facts_row['input_clock']
    assert clock['returned_at']==received and len(w.complete_cache.rows)==len(bars)
    for key in ('aggregation_started_at','aggregation_completed_at','core_step_started_at',
                'core_step_completed_at','observer_completed_at','core_result_enqueued_at'):
        assert datetime.fromisoformat(clock[key]).tzinfo is not None
    assert events[-1][0]=='CORE_READY'


def test_b_path_explanation_never_calls_background_macd_a_path_veto():
    from futures_trend_quant.live.paper_explain import lines
    text='\n'.join(lines(dict(path='B',gate='MACD_NOT_ALIGNED',permission=dict(new_entry_allowed=True,reasons=[]))))
    assert 'B路径M15柱符号仅提示' in text and '路径入场许可 True' in text


def test_h1_explanation_does_not_promote_logical_close_to_actual_receipt():
    from futures_trend_quant.live.paper_explain import h1_clock_context
    raw=dict(known_at='2026-11-13T22:00:00+08:00',complete=True,closed=True)
    out=h1_clock_context(raw)
    assert out['known_at'] is None and out['logical_closed_at']==raw['known_at']
    assert raw['known_at']=='2026-11-13T22:00:00+08:00'


@pytest.mark.parametrize('case,expected',[('valid',None),('pause','STOP_NEW_OPEN'),
    ('stale_clock','CLOCK'),('old_collector','CLOCK'),('expired','SIGNAL_EXPIRED'),
    ('service','SERVICE_RECOVERY_PENDING'),('quote','QUOTE_STALE')])
def test_r15_proof_through_real_entry_wal_and_fake_transport_keeps_every_guard(tmp_path,case,expected):
    import json
    from pathlib import Path
    from test_paper_execution import engine,signal,NOW
    from test_paper_runtime_health_v6 import setup_health,write
    e,t=engine(tmp_path);e.c['clock_probe_policy']=POLICY
    proof=setup_health(e.c,probe_policy=POLICY)
    ev=Path(e.c['evidence_dir']);s=signal()
    if case=='pause':e.s['pause_new']=True
    if case=='stale_clock':proof['sample_monotonic']-=91;write(ev/'clock_health.local.json',proof)
    if case=='old_collector':proof.pop('probe_policy');write(ev/'clock_health.local.json',proof)
    if case=='expired':s['signal_at']=(NOW-timedelta(seconds=31)).isoformat()
    if case=='service':
        p=ev/'service_health.local.json';v=json.loads(p.read_text());v['at']=(NOW-timedelta(seconds=4)).isoformat();write(p,v)
    if case=='quote':e.quotes['DCE.m2701']['created_at']=(NOW-timedelta(seconds=4)).isoformat()
    ok,reason=e.open(s,NOW)
    if case=='valid':
        assert ok and len(t.jobs)==1 and t.jobs[0]['op']=='submit'
        assert t.jobs[0]['id'] in json.loads(e.path.read_text())['intents']
        assert t.jobs[0]['args']['volume']==1 and t.jobs[0]['args']['price']==101
    else:
        assert not ok and expected in reason and not t.jobs


@pytest.mark.parametrize('case',['ok','running','gate','scope','account'])
def test_r15_installation_is_stopped_only_preserves_pause_and_checks_exact_policy(tmp_path,monkeypatch,case):
    import json
    from tools import paper_entry_r15_release as r
    from test_shutdown_r131_release import fixture
    from futures_trend_quant.live.paper_broker import atomic
    client,rt,bundle,plan,state=fixture(tmp_path,monkeypatch)
    profile=bundle/'client/_strategy_runtime'/r.PROFILE
    cfg=json.loads(profile.read_text());cfg.update(clock_probe_policy=POLICY,core_aggregation_policy='CORE_COMPLETE_BUCKET_CACHE_V1_20261114')
    atomic(profile,cfg);plan.update(version=r.VERSION,new_config_sha256=r.r8.base.prior.sha(profile));atomic(bundle/'release_plan.local.json',plan)
    gate=json.loads((bundle/'validation_gate.local.json').read_text());gate['config_sha256']=plan['new_config_sha256'];atomic(bundle/'validation_gate.local.json',gate)
    monkeypatch.setattr(r,'source_hashes',lambda _: {})
    monkeypatch.setattr(r,'stopped',lambda p:r.r8.base.stopped(p))
    before=(client/'main.py').read_bytes()
    if case=='running':monkeypatch.setattr(r,'stopped',lambda p:(_ for _ in ()).throw(ValueError('RUNNING')))
    if case=='gate':gate['tests_passed']=False;atomic(bundle/'validation_gate.local.json',gate)
    if case=='scope':cfg['max_lots']=99;atomic(profile,cfg);plan['new_config_sha256']=r.r8.base.prior.sha(profile);atomic(bundle/'release_plan.local.json',plan);gate['config_sha256']=plan['new_config_sha256'];atomic(bundle/'validation_gate.local.json',gate)
    if case=='account':monkeypatch.setattr(r.r8.base.prior,'flat_stopped',lambda p:(_ for _ in ()).throw(ValueError('ACCOUNT')))
    if case!='ok':
        with pytest.raises(ValueError):r.install(client,bundle)
        assert (client/'main.py').read_bytes()==before
    else:
        r.install(client,bundle)
        after=json.loads((rt/'state/paper_observation_five/execution.json').read_text())
        assert after==dict(state,config_sha256=plan['new_config_sha256']) and after['pause_new']
