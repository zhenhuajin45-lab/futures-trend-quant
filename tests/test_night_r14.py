from dataclasses import asdict,replace
from datetime import timedelta
from types import SimpleNamespace
from collections import Counter
import hashlib,json
import pandas as pd
import pytest
from test_gm_closed_m1 import GM,bar
from test_paper_execution import NOW
from futures_trend_quant.live.paper_gm_inputs import POLICY,persist
from futures_trend_quant.live.paper_closed_m1 import POLICY as CLOSED,restore
from tools.paper_notice_details import LocalBars


def put(p,x):
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x),encoding='utf8')


def setup(root,cs,symbol):
    state=root/'state/paper_observation_five/products'/cs.replace('.','_');state.mkdir(parents=True,exist_ok=True)
    c=dict(root=str(root),csymbol=cs,state_dir=str(state),gm_consumed_input_policy=POLICY)
    put(root/'config/paper_observation_five.local.json',c)
    seed=bar(symbol,end=NOW-pd.Timedelta(minutes=2));b=replace(seed,bob=seed.bob+timedelta(minutes=1),eob=seed.eob+timedelta(minutes=1))
    proof=persist(c,[seed],[],accepted_at=(NOW-timedelta(minutes=1,seconds=30)).isoformat())
    journal=state/'gm_core_m1.jsonl';raw=asdict(b);raw.update(bob=b.bob.isoformat(),eob=b.eob.isoformat())
    payload=dict(policy=CLOSED,csymbol=cs,bar=raw,source='GM_CLOSED_M1_HISTORY',source_known_at=(b.eob+timedelta(seconds=2)).isoformat(),recorded_at=(b.eob+timedelta(seconds=3)).isoformat())
    journal.write_text(json.dumps(payload)+'\n',encoding='utf8');blob=journal.read_bytes();prefix=dict(path=str(journal),bytes=len(blob),sha256=hashlib.sha256(blob).hexdigest())
    cp=dict(gm_input_cache=proof,last_eob=b.eob.isoformat(),gm_input_prefix=prefix)
    put(state/'core_checkpoint.json',cp)
    return c,state,seed,b,cp


@pytest.mark.parametrize('cs,symbol',GM)
def test_real_notice_loader_uses_core_committed_inputs_without_fake_manifest(tmp_path,cs,symbol):
    c,state,seed,b,cp=setup(tmp_path,cs,symbol);loader=LocalBars(tmp_path)
    assert not (tmp_path/'data/cache/paper_market'/(cs.replace('.','_')+'_consumed')/'input_manifest.json').exists()
    assert loader.load(cs,symbol,NOW)==[seed,b]
    assert loader.load(cs,symbol,b.eob+timedelta(seconds=2))==[seed]
    assert loader.sources[cs][1]['checkpoint_prefix']==cp['gm_input_prefix']
    assert loader.binding(cs,symbol)['seed_sha256']==cp['gm_input_cache']['sha256']
    # New appended data is not authorized by the current checkpoint prefix.
    with (state/'gm_core_m1.jsonl').open('a') as f:f.write('UNCOMMITTED_PARTIAL')
    assert loader.load(cs,symbol,NOW)==[seed,b]


@pytest.mark.parametrize('failure',['hash','prefix','symbol','future','window','clock','revision'])
def test_notice_consumer_fails_closed_for_invalid_source_or_availability(tmp_path,failure):
    cs,symbol=GM[0];c,state,seed,b,cp=setup(tmp_path,cs,symbol)
    if failure=='hash':cp['gm_input_cache']['sha256']='0'*64
    elif failure=='symbol':cp['gm_input_cache']['symbol']='DCE.m2705'
    elif failure=='future':cp['gm_input_cache']['accepted_at']=(NOW+timedelta(seconds=1)).isoformat()
    elif failure=='window':cp['gm_input_cache']['rows']=99
    else:
        p=state/'gm_core_m1.jsonl';v=json.loads(p.read_text())
        if failure=='prefix':v['source']='TAMPERED_SOURCE'
        if failure=='clock':v['source_known_at']=(b.eob-timedelta(seconds=1)).isoformat()
        if failure=='revision':v['bar'].update(bob=seed.bob.isoformat(),eob=seed.eob.isoformat(),close=100.5)
        p.write_text(json.dumps(v)+'\n');raw=p.read_bytes()
        if failure!='prefix':cp['gm_input_prefix'].update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    put(state/'core_checkpoint.json',cp)
    with pytest.raises(ValueError):LocalBars(tmp_path).load(cs,symbol,NOW)


def test_quote_reject_and_source_switch_preserve_full_original_clocks_with_bounded_sampling(tmp_path,monkeypatch):
    from futures_trend_quant.live import paper_quote_audit as a
    from futures_trend_quant.live.paper_five import FiveRunner
    from futures_trend_quant.live.paper_quote_feed import POLICY as Q
    mono=[100.];monkeypatch.setattr(a.time,'monotonic',lambda:mono[0])
    r=FiveRunner.__new__(FiveRunner);r.c=dict(quote_policy_version=Q,config_sha256='X');r.records={'DCE.m2701':{}};r.evidence=tmp_path;r.accepted_quotes={};r.quote_feed_counts=Counter();r.quote_feed_last={};r.engine=None;r.run_id='RUN'
    now=NOW.isoformat();q=dict(created_at=now,source='GM_BULK_CURRENT_POLL',bid=99,ask=100,tick=1)
    assert r.accept_quote('DCE.m2701',q,now)
    older=dict(q,created_at=(NOW-timedelta(seconds=2)).isoformat(),source='GM_HOST_TICK_CALLBACK')
    for _ in range(1000):assert not r.accept_quote('DCE.m2701',older,now)
    assert r.accepted_quotes['DCE.m2701']==q
    mono[0]+=5;assert not r.accept_quote('DCE.m2701',older,now)
    tick=dict(q,source='GM_HOST_TICK_CALLBACK');assert r.accept_quote('DCE.m2701',tick,now)
    rows=[json.loads(x) for x in (tmp_path/'quote_handoff.local.jsonl').read_text().splitlines()]
    assert len(rows)==4 and rows[1]['incoming']==older and rows[1]['previous']==q
    assert rows[2]['suppressed_same_outcome']==999 and rows[3]['source_switch']
    assert r.quote_feed_counts['OLDER_QUOTE_IGNORED']==1001


def test_source_flapping_cannot_generate_unbounded_audit_io(tmp_path,monkeypatch):
    from futures_trend_quant.live import paper_quote_audit as a
    monkeypatch.setattr(a.time,'monotonic',lambda:100.)
    r=SimpleNamespace(c={},evidence=tmp_path,run_id='TEST')
    for n in range(10000):
        source='GM_HOST_TICK_CALLBACK' if n%2 else 'GM_BULK_CURRENT_POLL'
        other='GM_BULK_CURRENT_POLL' if n%2 else 'GM_HOST_TICK_CALLBACK'
        a.record(r,'DCE.m2701',dict(source=source,created_at=NOW.isoformat()),NOW,dict(source=other),True)
    assert len((tmp_path/'quote_handoff.local.jsonl').read_text().splitlines())<=3


def test_clock_error_evidence_retains_exact_reason_without_relaxing_offset(monkeypatch):
    from futures_trend_quant.live.paper_clock_health import collect,SOURCES,MAX_OFFSET,MAX_AGE
    def sampler(src):return dict(source=src,offset_seconds=-.504 if src==SOURCES[0] else -.492,rtt_seconds=.2)
    x=collect('C','R',sampler);assert x['status']=='UNTRUSTED' and x['reason']=='CLOCK_OFFSET'
    def failed(src):
        e=OSError('DNS');e.probe_stage='DNS_RESOLVE';raise e
    y=collect('C','R',failed);assert y['reason']=='CLOCK_SOURCE_QUORUM' and all(e['stage']=='DNS_RESOLVE' and e['duration_seconds']>=0 for e in y['errors'])
    assert MAX_OFFSET==.5 and MAX_AGE==90


@pytest.mark.parametrize('received',[None,'UNKNOWN','2026-11-05T21:00:00',(NOW+timedelta(seconds=1)).isoformat(),NOW.isoformat()])
def test_observer_preserves_only_real_aware_receive_clock(received):
    from futures_trend_quant.live.trend_observation import attach_input_clock
    clocks=dict(returned_at=received,source='GM_CLOSED_M1_HISTORY');out=attach_input_clock({},clocks,NOW.isoformat())
    assert out['first_received_at']==(received if received==NOW.isoformat() else None)
    assert out['computed_at']==NOW.isoformat() and out['input_clock']==clocks


def test_release_wrapper_does_not_install_another_version_or_clear_pause(tmp_path,monkeypatch):
    from tools import paper_night_r14_release as r14
    from tools import paper_shutdown_r131_release as old_release
    from test_shutdown_r131_release import fixture
    client,rt,bundle,plan,state=fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(r14,'source_hashes',old_release.source_hashes)
    before=(client/'main.py').read_bytes()
    with pytest.raises(ValueError,match='R14_RELEASE_IDENTITY'):r14.install(client,bundle)
    assert (client/'main.py').read_bytes()==before
    plan['version']=r14.VERSION;put(bundle/'release_plan.local.json',plan)
    receipt=r14.install(client,bundle)
    actual=json.loads((rt/'state/paper_observation_five/execution.json').read_text())
    assert actual==dict(state,config_sha256=plan['new_config_sha256']) and actual['pause_new']
    assert receipt['self_check']==dict(result='SYNTHETIC')


def test_prepare_rejects_old_profile_without_canonical_consumed_contract(tmp_path,monkeypatch):
    from tools import paper_night_r14_release as r14
    monkeypatch.setattr(r14.r8,'validate_active_sources',lambda _:None)
    put(tmp_path/'client/_strategy_runtime'/r14.PROFILE,dict(universe=list(r14.TEN_POOL),entry_policy_version='PAPER_ENTRY_V5'))
    with pytest.raises(ValueError,match='R14_REQUIRES_GM_CONSUMED_INPUT_PROFILE'):
        r14.prepare(tmp_path/'client',tmp_path/'bundle')
    assert not (tmp_path/'bundle').exists()


@pytest.mark.parametrize('cs,symbol',GM)
def test_complete_m5_poll_consumes_canonical_cache_and_preserves_execution(tmp_path,cs,symbol):
    from test_paper_notice_details import fixture,NOW as notice_now
    from tools.paper_notice_details import M5DivergenceMonitor
    root,_=fixture(tmp_path);c,state,seed,b,cp=setup(root,cs,symbol)
    # Production-shaped closed M1 inputs through the real consumer and real
    # M5 calculations. Quotes here are labelled synthetic, no trading API.
    end=pd.Timestamp(notice_now).floor('5min')
    bars=[bar(symbol,end=end-pd.Timedelta(minutes=250-i)) for i in range(250)]
    c.update(base_strategy='config/base.json',signal_max_age_seconds=30)
    put(root/'config/paper_observation_five.local.json',c)
    cp['gm_input_cache']=persist(c,bars,[],accepted_at=(notice_now-timedelta(minutes=1)).isoformat())
    last=bar(symbol,end=end);raw=asdict(last);raw.update(bob=last.bob.isoformat(),eob=last.eob.isoformat())
    p=state/'gm_core_m1.jsonl';p.write_text(json.dumps(dict(policy=CLOSED,csymbol=cs,bar=raw,source_known_at=(end+pd.Timedelta(seconds=2)).isoformat(),recorded_at=(end+pd.Timedelta(seconds=3)).isoformat()))+'\n')
    data=p.read_bytes();cp.update(last_eob=end.isoformat(),gm_input_prefix=dict(path=str(p),bytes=len(data),sha256=hashlib.sha256(data).hexdigest()),runtime=dict(trends={cs:dict(trend_state='bull')}));put(state/'core_checkpoint.json',cp)
    ev=root/'evidence/paper_observation_five';put(ev/'status.json',dict(at=notice_now.isoformat(),products={cs:dict(symbol=symbol,ready=True)}))
    put(root/'state/paper_observation_five/market_registry.local.json',dict(contracts={symbol:dict(trade_windows=[(notice_now.timestamp()-20000,notice_now.timestamp()+3600)])}))
    put(ev/'m5_divergence_cursor.local.json',dict(cutoff=(notice_now-timedelta(minutes=5)).isoformat(),processed=['KEEP_OLD_TOMBSTONE'],last_checked={}))
    before=(root/'state/paper_observation_five/execution.json').read_bytes()
    monitor=M5DivergenceMonitor(tmp_path);monitor.poll(notice_now)
    health=json.loads((ev/'m5_divergence_health.local.json').read_text());cursor=json.loads((ev/'m5_divergence_cursor.local.json').read_text())
    assert health['status']=='RUNNING' and health['errors']=={} and health['last_checked'][cs]==int(end.timestamp())
    assert cursor['processed']==['KEEP_OLD_TOMBSTONE'] and (root/'state/paper_observation_five/execution.json').read_bytes()==before
