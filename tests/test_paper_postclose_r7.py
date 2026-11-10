"""Real IO/progress paths and isolated observer; no trading/network side effects."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock
import json
import os
import queue
import threading
import math
import pytest
from futures_trend_quant.live import paper_io
from futures_trend_quant.live.paper_broker import atomic
from futures_trend_quant.live.paper_progress import TaskProgress,progress_problem
from futures_trend_quant.live.paper_five import FiveRunner
from futures_trend_quant.core.models import Bar
from futures_trend_quant.observation.price_action.models import DEFAULTS,Zone,PivotFact,digest,parameters
from futures_trend_quant.observation.price_action.structure import structure,confirmed_pivots
from futures_trend_quant.observation.price_action.zones import pivot_zone,position,touch,short_box
from futures_trend_quant.observation.price_action.patterns import quality
from futures_trend_quant.observation.price_action.breakout import advance
from futures_trend_quant.observation.price_action.room import available_room
from futures_trend_quant.observation.price_action.state import State
from futures_trend_quant.observation.price_action.pipeline import Pipeline

NOW=datetime(2026,11,10,15,tzinfo=timezone(timedelta(hours=8)))


def bar(i=0,o=100,h=103,l=98,c=101,volume=10,symbol='DCE.m2701'):
    end=NOW+timedelta(minutes=15*i)
    return Bar(symbol,'900s',end-timedelta(minutes=15),end,o,h,l,c,volume)


def pivot(kind,price,i):
    at=(NOW-timedelta(minutes=15*(20-i))).isoformat()
    return PivotFact(str(i),str(i),kind,price,at,at,at,2)


def test_io_retries_only_transient_and_diagnostic_never_contains_original_path(tmp_path,monkeypatch):
    path=tmp_path/'clock_health.local.json';atomic(path,dict(status='HEALTHY'))
    original=paper_io.shared_open;calls=[]
    def transient(p):
        calls.append(p)
        if len(calls)<3:raise PermissionError(13,'sensitive-directory')
        return original(p)
    monkeypatch.setattr(paper_io,'shared_open',transient)
    assert paper_io.read_json(path)['status']=='HEALTHY' and len(calls)==3
    monkeypatch.setattr(paper_io,'shared_open',Mock(side_effect=PermissionError(13,'secret')))
    with pytest.raises(paper_io.JsonReadError) as error:paper_io.read_json(path)
    assert error.value.details['attempts']==3 and error.value.details['file']==path.name
    assert str(tmp_path) not in str(error.value) and 'secret' not in str(error.value.details)
    path.write_text('{')
    monkeypatch.setattr(paper_io,'shared_open',original)
    with pytest.raises(paper_io.JsonReadError) as error:paper_io.read_json(path)
    assert error.value.details['stage']=='DECODE_JSON' and error.value.details['attempts']==1


@pytest.mark.skipif(os.name!='nt',reason='actual Windows DELETE sharing')
def test_windows_reader_allows_writer_atomic_replace_while_read_handle_is_open(tmp_path):
    path=tmp_path/'proof.json';atomic(path,dict(value=1))
    with paper_io.shared_open(path) as old:
        atomic(path,dict(value=2))
        assert json.loads(old.read())['value']==1
        assert paper_io.read_json(path)['value']==2


def test_jsonl_tail_partial_rewrite_and_append_are_not_silently_lost(tmp_path):
    path=tmp_path/'input.jsonl';tail=paper_io.JsonlTail()
    path.write_text('{"value":1}\n{"value":')
    assert tail.read(path)==[dict(value=1)]
    with path.open('a') as f:f.write('2}\n')
    assert tail.read(path)==[dict(value=2)]
    path.write_text('{"value":12345}\n')
    assert tail.read(path)==[dict(value=12345)]


def test_progress_uses_real_stage_and_keeps_failure_until_same_stage_recovers(tmp_path):
    p=TaskProgress(tmp_path/'health.json','TEST')
    with pytest.raises(ValueError):p.run('M5',lambda:(_ for _ in ()).throw(ValueError('x')))
    p.run('OTHER',lambda:None)
    h=paper_io.read_json(p.path)
    assert 'STAGE_FAILED:M5' in progress_problem(h,os.getpid(),datetime.now(timezone.utc))
    p.run('M5',lambda:None)
    h=paper_io.read_json(p.path)
    assert progress_problem(h,os.getpid(),datetime.now(timezone.utc)) is None
    h.update(status='RUNNING',stage_started_at=(datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat())
    assert 'PROGRESS_STALE' in progress_problem(h,os.getpid(),datetime.now(timezone.utc))


def test_pause_cause_history_dedupes_and_migration_does_not_invent_first_cause(tmp_path):
    from test_paper_execution import engine
    e,t=engine(tmp_path);e.s.update(pause_new=True,pause_metadata=dict(reason='OLD',source='CLOCK',at=str(NOW)))
    e.pause_entries('NEW',NOW,'NOTIFIER');e.pause_entries('NEW',NOW,'NOTIFIER')
    assert len(e.s['pause_cause_history'])==2 and e.s['pause_first_known']['reason']=='OLD'
    assert 'UNKNOWN' in e.s['pause_first_known']['basis'] and e.s['pause_new'] and not t.jobs


@pytest.mark.parametrize('prices,expected',[
    ([100,90,110,95],'UP'),([110,100,105,90],'DOWN'),([100,90,100,90],'TRANSITION'),([100,90,110,85],'TRANSITION')])
def test_confirmed_structure_and_damage_do_not_auto_reverse(prices,expected):
    ps=[pivot(k,p,i) for i,(k,p) in enumerate(zip(['high','low','high','low'],prices))]
    result=structure(ps,99,1)
    assert result['structure_state'].removesuffix('_DAMAGED')==expected
    if expected=='UP':assert structure(ps,94,1)['structure_state']=='UP_DAMAGED'
    assert structure(ps[:2],99,1)['structure_state']=='UNKNOWN'


def test_zone_width_frozen_touch_dedup_and_middle_is_label_only():
    z=pivot_zone(pivot('high',102,1),1,DEFAULTS)
    assert (z.lower,z.upper)==(100,104)
    a=touch(z,bar(),{});b=touch(z,bar(1),a)
    assert a['count']==b['count']==1
    outside=touch(z,bar(2,o=110,h=112,l=109,c=111),b)
    assert touch(z,bar(3),outside)['count']==2
    assert position(100,dict(status='FORMED',lower=90,upper=110),DEFAULTS)['location_class']=='MIDDLE'
    assert position(100,dict(status='UNKNOWN'),DEFAULTS)['position'] is None


def test_breakout_clocks_false_break_retest_and_idempotence():
    z=Zone('Z','M15_PIVOT','RESISTANCE',99,101,('P',),(NOW-timedelta(hours=1)).isoformat(),(NOW-timedelta(hours=1)).isoformat())
    first=advance(z,bar(o=100,h=104,l=99,c=103),1,known_at=NOW.isoformat())
    assert first['status']=='CLOSE_ACCEPTED'
    assert advance(z,bar(o=100,h=104,l=99,c=103),1,first)==first
    failed=advance(z,bar(1,o=103,h=104,l=99,c=100),1,first,known_at=(NOW+timedelta(minutes=15)).isoformat())
    assert failed['status']=='FAILED_ACCEPTANCE' and not failed['signal_generated']
    held=advance(z,bar(1,o=103,h=104,l=101,c=103),1,first)
    assert held['status']=='RETEST_HELD' and held['role_change']=='SUPPORT'
    assert advance(z,bar(o=100,h=104,l=99,c=100),1)['status']=='CLOSED_BACK'
    late=Zone('L','P','RESISTANCE',99,101,(),NOW.isoformat(),NOW.isoformat())
    assert advance(late,bar(),1)['status']=='UNKNOWN'


def test_pattern_zero_body_range_no_trade_and_nested_mother():
    assert quality(bar(o=100,h=100,l=100,c=100))['status']=='UNKNOWN'
    assert quality(bar(volume=0))['reason']=='NO_TRADE_OR_NORMALIZED_BAR'
    assert quality(bar(o=100,c=100))['reason']=='ZERO_BODY_NO_AUTOMATIC_PIN'
    mother=bar(o=100,h=110,l=90,c=101)
    first=bar(1,o=100,h=105,l=95,c=101)
    second=bar(2,o=100,h=104,l=96,c=101)
    assert quality(first,mother)['mother_id']==quality(second,first,mother=mother)['mother_id']==mother.identity


@pytest.mark.parametrize('direction,entry,stop,role,low,high,space',[
    ('bull',100,95,'RESISTANCE',110,112,100),('bear',100,105,'SUPPORT',88,90,100),
    ('bull',111,106,'RESISTANCE',110,112,0)])
def test_room_uses_near_edge_costs_and_no_obstacle_is_never_infinite(direction,entry,stop,role,low,high,space):
    z=Zone('Z','P',role,low,high,(),(NOW-timedelta(hours=1)).isoformat(),(NOW-timedelta(hours=1)).isoformat())
    kwargs=dict(coverage=True,quote_known=True,costs=dict(win=2,loss=2,effective_contract_date='EXPLICIT_TEST_FEE',entry_spread_counted_twice=False))
    result=available_room(direction,entry,stop,10,[z],NOW,**kwargs)
    assert result['space_gross']==space and result['rr_space_net']==(space-2)/52
    assert available_room(direction,entry,stop,10,[],NOW,**kwargs)['status']=='NO_OBSTACLE_OBSERVED'
    kwargs['costs']=None
    assert available_room(direction,entry,stop,10,[z],NOW,**kwargs)['rr_space_net'] is None


def test_pipeline_persistence_future_input_identity_and_candidate_link(tmp_path):
    state=State(tmp_path,digest(DEFAULTS));p=Pipeline(state)
    bars=[bar(i-40,o=100+i%4,h=105+i%4,l=95+i%4,c=101+i%4) for i in range(40)]
    received={b.identity:(b.eob+timedelta(seconds=1)).isoformat() for b in bars}
    args=dict(symbol='DCE.m2701',csymbol='DCE.M',m15=bars,m5=[],received=received,tick=1,asof=NOW,
              trading_day='2026-11-10',session_id='REGISTERED_TEST',source_prefix_sha256='a'*64)
    result=p.snapshot(**args)
    assert result['mode']=='SHADOW' and not result['execution_changed']
    assert p.snapshot(**args) is None
    resumed=Pipeline(State(tmp_path,digest(DEFAULTS)))
    assert resumed.snapshot(**args) is None
    candidate=dict(candidate_evidence_id='ORIGINAL_B',direction='bear',path='B',entry=100,stop=105,multiplier=10,original_expires_at='UNCHANGED')
    linked=resumed.snapshot(**args,candidate=candidate)
    assert linked['candidate_evidence_id']=='ORIGINAL_B' and linked['original_plan']==candidate
    with pytest.raises(ValueError,match='FUTURE_BAR'):p.snapshot(**dict(args,m15=bars+[bar(1)]))
    with pytest.raises(ValueError,match='CONTRACT_IDENTITY'):p.snapshot(**dict(args,symbol='DCE.y2701'))
    with pytest.raises(ValueError):parameters(dict(mode='ENFORCE'))


def test_shadow_queue_failure_cannot_change_original_engine_or_exit(tmp_path):
    from types import SimpleNamespace
    from test_paper_execution import engine
    e,t=engine(tmp_path);before=json.dumps(e.s,sort_keys=True,default=str)
    r=SimpleNamespace(engine=e,price_action_observer=SimpleNamespace(offer=Mock(side_effect=queue.Full()),dropped=0))
    FiveRunner.observe(r,'BARS',csymbol='DCE.M')
    assert r.price_action_observer.dropped==1 and json.dumps(e.s,sort_keys=True,default=str)==before and not t.jobs


def test_shared_io_at_clock_final_guard_keeps_fail_closed_and_reports_file(tmp_path,monkeypatch):
    from test_paper_runtime_health_v6 import setup_health
    from futures_trend_quant.live.paper_clock_health import health
    c=dict(root=str(tmp_path),evidence_dir=str(tmp_path/'ev'),config_sha256='HASH',clock_policy_version='PAPER_CLOCK_HEALTH_V1_20261109')
    setup_health(c)
    assert health(c)['reason'] is None
    original=paper_io.shared_open
    def fail(path):
        if Path(path).name=='run_manifest.json':raise PermissionError(13,'secret')
        return original(path)
    monkeypatch.setattr(paper_io,'shared_open',fail)
    result=health(c)
    assert result['reason']=='SYSTEM_CLOCK_UNTRUSTED' and result['diagnostic']['file']=='run_manifest.json'


def test_causal_pivot_confirmation_and_immutable_historical_snapshot(tmp_path):
    bars=[]
    for i in range(120):
        v=100+20*math.sin(i*math.pi/6)
        bars.append(bar(i-120,o=v,h=v+1,l=v-1,c=v+.5))
    received={b.identity:(b.eob+timedelta(seconds=1)).isoformat() for b in bars}
    prefix=bars[:95];at=prefix[-1].eob+timedelta(seconds=2)
    first=confirmed_pivots(prefix,received,at)
    assert len(first)>4
    for p in first:
        assert datetime.fromisoformat(p.confirmed_at)>datetime.fromisoformat(p.occurred_at)
        before=datetime.fromisoformat(p.confirmed_at)-timedelta(microseconds=1)
        assert p.event_id not in {x.event_id for x in confirmed_pivots(prefix,received,before)}
    p=Pipeline(State(tmp_path,digest(DEFAULTS)))
    args=dict(symbol=prefix[-1].symbol,csymbol='DCE.M',m15=prefix,m5=[],received=received,tick=1,asof=at,
        trading_day='2026-11-10',session_id='TEST',source_prefix_sha256='0'*64)
    original=p.snapshot(**args);serialized=json.dumps(original,sort_keys=True)
    p.snapshot(**dict(args,m15=bars,asof=NOW+timedelta(seconds=2)))
    assert json.dumps(original,sort_keys=True)==serialized
    assert State(tmp_path,digest(DEFAULTS)).data['pending_results'][original['event_id']]==json.loads(serialized)
    with pytest.raises(ValueError,match='FUTURE_RECEIVE_CLOCK'):
        p.snapshot(**dict(args,received={b.identity:(at+timedelta(seconds=1)).isoformat() for b in prefix}))


def test_previous_trading_day_zone_requires_exchange_complete_coverage():
    from futures_trend_quant.observation.price_action.zones import previous_day_zones
    b=[bar(-4),bar(-3),bar(-2)]
    days={b[0].identity:'2026-11-09',b[1].identity:'2026-11-09',b[2].identity:'2026-11-10'}
    assert not previous_day_zones(b,days,[],'2026-11-10',1,2,NOW.isoformat(),DEFAULTS)
    zones=previous_day_zones(b,days,['2026-11-09'],'2026-11-10',1,2,NOW.isoformat(),DEFAULTS)
    assert len(zones)==2 and b[2].identity not in zones[0].source_ids


def test_mature_box_requires_clustered_independent_confirmed_pivots():
    from futures_trend_quant.observation.price_action.zones import mature_range
    b=[bar(i-21) for i in range(20)]
    ps=[pivot(k,p,i) for i,(k,p) in enumerate([('high',105),('low',95),('high',105),('low',95)])]
    assert mature_range(b,ps,NOW,DEFAULTS)['status']=='FORMED'
    assert mature_range(b,ps[:2],NOW,DEFAULTS)['status']=='NOT_FORMED'
    assert mature_range(b[:5],ps,NOW,DEFAULTS)['status']=='UNKNOWN'


def test_observer_state_identity_never_reuses_different_parameters(tmp_path):
    state=State(tmp_path,digest(DEFAULTS));state.save()
    with pytest.raises(ValueError,match='IDENTITY'):State(tmp_path,'OTHER_PARAMETERS')


def test_shadow_replay_and_report_are_runnable_without_fill_claim(tmp_path):
    from dataclasses import asdict
    from tools.price_action_shadow_replay import replay
    from tools.price_action_shadow_report import report
    b=[bar(i-31) for i in range(30)]
    r=dict(symbol=b[-1].symbol,csymbol='DCE.M',m15=[asdict(x) for x in b],m5=[],
        received={x.identity:(x.eob+timedelta(seconds=1)).isoformat() for x in b},tick=1,asof=NOW.isoformat(),
        trading_day='2026-11-10',session_id='EXPLICIT_TEST',source_prefix_sha256='0'*64,
        availability_basis='CAUSAL_REPLAY_SIMULATED_COMPUTE_AT_RECEIPT')
    path=tmp_path/'input.jsonl';path.write_text(json.dumps(r,default=str)+'\n',encoding='utf8')
    result=replay(path,tmp_path/'output');assert result['snapshots']==1 and result['orders_sent']==0
    stats=report(tmp_path/'output',tmp_path/'report.json')
    assert not stats['coverage_floor_met'] and not stats['economic_edge_verified']


@pytest.mark.skipif(os.name!='nt',reason='actual Windows long evidence path')
def test_shadow_atomic_output_at_realistic_long_windows_evidence_path(tmp_path):
    from futures_trend_quant.observation.price_action.state import atomic as shadow_atomic
    path=tmp_path/('nested_'+'x'*65)/('source_'+'y'*65)/('e'*64+'.local.json')
    assert len(str(path))>260
    shadow_atomic(path,dict(event_id='e'*64,value=1))
    assert paper_io.read_json(path)['value']==1
    shadow_atomic(path,dict(event_id='e'*64,value=2))
    assert paper_io.read_json(path)['value']==2


def test_short_box_matches_original_c_geometry_at_same_m5_prefix():
    from dataclasses import replace
    from futures_trend_quant.core.facts.indicators import calculate_indicators
    from futures_trend_quant.core.entry.evidence import _consolidation_and_breakout
    bars=[]
    for i in range(50):
        end=NOW-timedelta(minutes=5*(50-i))
        p=100+i*.1
        bars.append(Bar('DCE.m2701','300s',end-timedelta(minutes=5),end,p,p+.5,p-.5,p+.1,10))
    # Full prefix is the same context used by the original C implementation.
    probe=replace(bars[-1],bob=bars[-1].eob,eob=bars[-1].eob+timedelta(minutes=5),high=110,close=109)
    full=bars+[probe];ind=calculate_indicators(full)
    original,_=_consolidation_and_breakout(full,ind,'bull',4,1.5)
    box=short_box(bars,1000,DEFAULTS)  # M15 ATR must not control the C box width.
    assert original is not None and original.bar_id==bars[-1].identity
    assert box['status']=='FORMED' and box['direction']=='bull' and box['threshold_atr']==1.5
    assert box['source_ids']==[b.identity for b in bars[-4:]]


def test_invalidation_records_touch_wick_and_close_without_assuming_a_position():
    from futures_trend_quant.observation.price_action.invalidation import observe
    original=dict(candidate_evidence_id='ORIGINAL_B',path='B',direction='bull',anchor=99,anchor_bar_id='ORIGINAL',original_expires_at='FIXED')
    touched=observe(original,bar(l=99),NOW.isoformat())
    assert touched['status']=='TOUCHED' and not touched['closed_broken']
    wick=observe(original,bar(1,l=98),NOW.isoformat(),touched)
    assert wick['status']=='WICK_CROSSED' and not wick['actual_position_assumed']
    broken=observe(original,bar(2,o=100,h=101,l=97,c=98),NOW.isoformat(),wick)
    assert broken['status']=='CLOSED_INVALIDATED' and broken['original_expires_at']=='FIXED'
    assert observe(original,bar(3),NOW.isoformat(),broken)['status']=='CLOSED_INVALIDATED'
    assert not broken['actual_exit_changed']
