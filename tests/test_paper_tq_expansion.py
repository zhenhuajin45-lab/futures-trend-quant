"""Ten-product source admission and real coordinator -> WAL -> native cache path."""
from collections import Counter
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
import json
import queue
import pytest
import pandas as pd
from futures_trend_quant.live import paper_five
from futures_trend_quant.live.paper_five import FiveRunner
from futures_trend_quant.live.paper_tq import POLICY,SOURCE,atomic,append,quote_values,validate_envelope,validate_config
from futures_trend_quant.live.paper_tq_market import tq_rows,merge_same_source
from futures_trend_quant.live.paper_products import TQ_ADDED,TEN_POOL,TEN_POOL_VERSION,OILS_POOL,registered_pool,product_of
from futures_trend_quant.live.paper_quote_cache import latest_quote
from futures_trend_quant.live.paper_calendar import PaperCalendar
from test_paper_execution import engine,signal,snapshot,NOW,SESSIONS

SYMBOLS=['CZCE.MA701','DCE.eg2701','DCE.jd2612','CZCE.CJ701','SHFE.ag2702']


def fixture(tmp_path,symbol):
    e,t=engine(tmp_path);e.c.update(root=str(tmp_path),universe=list(TEN_POOL),pool_policy_version=TEN_POOL_VERSION,
        external_market_policy=POLICY,quote_policy_version='GM_HOST_TICK_PRIMARY_V1_20261109')
    from test_paper_runtime_health_v6 import setup_health
    from futures_trend_quant.live.paper_service_health import POLICY as SERVICE_POLICY
    health=setup_health(e.c);health['run_id']='GEN';atomic(Path(e.c['evidence_dir'])/'clock_health.local.json',health)
    service=Path(e.c['evidence_dir'])/'service_health.local.json';state=json.loads(service.read_text());state['run_id']='GEN';atomic(service,state)
    proof=dict(policy=POLICY,config_sha256='SYNTHETIC',run_id='GEN',status='RUNNING',updated_at=str(NOW),contracts={product_of(symbol):symbol},main_mapping={product_of(symbol):symbol})
    atomic(Path(e.c['evidence_dir'])/'tq_feed/current_run.json',proof)
    append(Path(e.c['evidence_dir'])/'tq_feed/GEN/health.jsonl',proof)
    atomic(Path(e.c['evidence_dir'])/'run_manifest.json',dict(run_id='GEN',config_sha256='SYNTHETIC',service_policy_version=SERVICE_POLICY))
    r=FiveRunner.__new__(FiveRunner);r.c=e.c;r.engine=e;r.records={symbol:dict(tick=1)}
    r.evidence=Path(e.c['evidence_dir']);r.accepted_quotes={};r.quote_feed_counts=Counter();r.quote_feed_last={}
    r.products={};r.host_ticks=queue.Queue();r.quote_events=queue.Queue()
    return r,e,t,proof


@pytest.mark.parametrize('symbol',SYMBOLS)
@pytest.mark.parametrize('direction',['bull','bear'])
def test_tq_source_reaches_real_wal_and_same_final_native_guard(tmp_path,monkeypatch,symbol,direction):
    r,e,t,proof=fixture(tmp_path,symbol)
    e.c.update(strategy_name='S0_SHORT_PAPER_CORE_V2',entry_policy_version='PAPER_ENTRY_V5',execution_policy_version='PAPER_EXEC_V5')
    monkeypatch.setattr(paper_five,'pd',SimpleNamespace(Timestamp=SimpleNamespace(now=lambda **k:NOW)))
    raw=dict(datetime=NOW.strftime('%Y-%m-%d %H:%M:%S.%f'),last_price=101 if direction=='bull' else 100,
        bid_price1=100,ask_price1=101,bid_volume1=10,ask_volume1=12)
    q=quote_values(raw,dict(price_tick=1),'GEN')
    r.quote_events.put(('QUOTE',symbol,q,str(NOW)));r.market_events(NOW)
    assert e.quotes[symbol]['source']==SOURCE and not t.jobs
    s=signal();s.update(symbol=symbol,direction=direction,anchor=99 if direction=='bull' else 103,
        anchor_bar_id=symbol+'|900s|'+(NOW-pd.Timedelta(minutes=30)).isoformat(),
        core_evidence_id='E1',entry_permission=True,permission_policy='PAPER_ENTRY_V5',permission_direction=direction,
        permission_eob=NOW.isoformat(),evidence_created_at=NOW.isoformat(),first_eligible_at=NOW.isoformat(),decision_at=NOW.isoformat(),
        expires_at=(NOW+pd.Timedelta(seconds=30)).isoformat(),reference_opponent_price=101 if direction=='bull' else 100)
    s['entry_policy_proof']=dict(policy='PAPER_ENTRY_V5',path='D',evidence_id='E1',eob=NOW.isoformat(),allowed=True,reasons=[],
        hist=1 if direction=='bull' else -1,close_side='above' if direction=='bull' else 'below',slope='up' if direction=='bull' else 'down',
        opposite_divergence=False,closed_h1_known=True,calendar_qualified=True,h1_absolute=direction+'_strong',
        h1_source=dict(closed=True,calendar_qualified=True,entry_eob=(NOW-pd.Timedelta(hours=1)).isoformat()))
    assert e.offer(s,NOW)==(False,'CANDIDATE_WAITING')
    e.drain_candidates(NOW)
    assert t.jobs
    job=t.jobs[-1];assert job['op']=='submit'
    persisted=json.loads(e.path.read_text());assert job['id'] in persisted['intents']
    result=latest_quote(tmp_path,e.c,'SYNTHETIC',job['args'],NOW.to_pydatetime())
    assert result['source']==SOURCE and result['limit_price_unchanged']==job['args']['price']
    assert job['args']['entry_policy_proof']==s['entry_policy_proof'] and result['source_run_id']=='GEN'


@pytest.mark.parametrize('mutation',['source','profile','generation','stale_proof','stale_event','stale_receive','spread','size','wrong_tick','broken_anchor','unknown_main'])
def test_final_native_guard_rejects_external_bad_facts(tmp_path,mutation):
    symbol=SYMBOLS[0];r,e,t,proof=fixture(tmp_path,symbol)
    q=dict(created_at=str(NOW),last=101,bid=100,ask=101,bid_size=3,ask_size=4,tick=1,source=SOURCE,source_run_id='GEN')
    row=dict(symbol=symbol,config_sha256='SYNTHETIC',received_at=str(NOW),quote=q)
    args=dict(symbol=symbol,side=1,position_effect=1,tick=1,price=101,anchor=99)
    if mutation=='source':q['source']='GM_HOST_TICK_CALLBACK'
    elif mutation=='profile':row['config_sha256']='OLD'
    elif mutation=='generation':q['source_run_id']='OLD'
    elif mutation=='stale_proof':proof['updated_at']=str(NOW-pd.Timedelta(seconds=4))
    elif mutation=='stale_event':q['created_at']=str(NOW-pd.Timedelta(seconds=4))
    elif mutation=='stale_receive':row['received_at']=str(NOW-pd.Timedelta(seconds=4))
    elif mutation=='spread':q['ask']=106
    elif mutation=='size':q['ask_size']=0
    elif mutation=='wrong_tick':q['tick']=5
    elif mutation=='unknown_main':proof['main_mapping']={}
    else:q['last']=98
    atomic(Path(e.c['evidence_dir'])/'tq_feed/current_run.json',proof)
    append(Path(e.c['evidence_dir'])/'tq_feed/GEN/health.jsonl',proof)
    atomic(Path(e.c['evidence_dir'])/'latest_quotes'/(symbol+'.json'),row)
    with pytest.raises(ValueError):latest_quote(tmp_path,e.c,'SYNTHETIC',args,NOW.to_pydatetime())
    assert not t.jobs


def test_unknown_main_mapping_keeps_fresh_pinned_exit_quote_available(tmp_path):
    symbol=SYMBOLS[0];r,e,t,proof=fixture(tmp_path,symbol);proof['main_mapping']={}
    append(Path(e.c['evidence_dir'])/'tq_feed/GEN/health.jsonl',proof)
    q=dict(created_at=str(NOW),last=101,bid=100,ask=101,bid_size=3,ask_size=4,tick=1,source=SOURCE,source_run_id='GEN')
    atomic(Path(e.c['evidence_dir'])/'latest_quotes'/(symbol+'.json'),dict(symbol=symbol,config_sha256='SYNTHETIC',received_at=str(NOW),quote=q))
    assert latest_quote(tmp_path,e.c,'SYNTHETIC',dict(symbol=symbol,side=2,position_effect=2,tick=1,price=100),NOW.to_pydatetime())['source']==SOURCE


def test_final_quote_deadline_includes_source_proof_read_time(tmp_path,monkeypatch):
    from futures_trend_quant.live import paper_quote_cache,paper_tq
    symbol=SYMBOLS[0];r,e,t,proof=fixture(tmp_path,symbol)
    q=dict(created_at=str(NOW-pd.Timedelta(seconds=2)),last=101,bid=100,ask=101,bid_size=3,ask_size=4,tick=1,source=SOURCE,source_run_id='GEN')
    atomic(Path(e.c['evidence_dir'])/'latest_quotes'/(symbol+'.json'),dict(symbol=symbol,config_sha256='SYNTHETIC',received_at=str(NOW),quote=q))
    monkeypatch.setattr(paper_tq,'final_source',lambda *a,**k:None)
    ticks=iter([0.,0.,2.])
    monkeypatch.setattr(paper_quote_cache,'time',SimpleNamespace(monotonic=lambda:next(ticks)))
    with pytest.raises(ValueError,match='stale/future'):
        latest_quote(tmp_path,e.c,'SYNTHETIC',dict(symbol=symbol,side=1,position_effect=1,tick=1,price=101,anchor=99),NOW.to_pydatetime())


def test_authorized_ten_pool_does_not_admit_arbitrary_product_mix():
    assert registered_pool(list(TEN_POOL),TEN_POOL_VERSION)==TEN_POOL
    for pool,version in [(list(TEN_POOL),None),(list(OILS_POOL)+['CZCE.MA'],TEN_POOL_VERSION),([*TEN_POOL[:-1],'CZCE.SR'],TEN_POOL_VERSION)]:
        with pytest.raises(ValueError):registered_pool(pool,version)
    assert product_of('SHFE.ag2702')=='SHFE.AG'


@pytest.mark.parametrize('key,value',[('run_id','OLD'),('symbol','DCE.m2701'),('config_sha256','OLD'),('updated_at',str(NOW-pd.Timedelta(seconds=4)))])
def test_external_envelope_never_reuses_old_generation_or_clock(key,value):
    c=dict(config_sha256='C',tq_run_id='R');row=dict(policy=POLICY,config_sha256='C',run_id='R',symbol=SYMBOLS[0],updated_at=str(NOW));row[key]=value
    with pytest.raises(ValueError):validate_envelope(row,c,SYMBOLS[0],NOW)


def payload(bob,**changes):
    ns=pd.Timestamp(bob).value
    raw=dict(datetime=ns,open=100,high=102,low=99,close=101,volume=10,close_oi=200);raw.update(changes)
    return dict(forming_bob_ns=ns+60_000_000_000,rows=[raw])


def test_forming_bar_and_revisions_are_never_promoted():
    p=payload('2026-11-05T09:00:00+08:00');symbol=SYMBOLS[0]
    bars=tq_rows(p,symbol)
    assert bars[0].eob.isoformat()=='2026-11-05T09:01:00+08:00'
    with pytest.raises(ValueError):tq_rows(dict(p,forming_bob_ns=p['rows'][0]['datetime']),symbol)
    cal=PaperCalendar(['2026-11-04','2026-11-05','2026-11-06','2026-11-09'],SESSIONS,'CZCE.MA')
    with pytest.raises(ValueError,match='REVISION'):merge_same_source(bars,tq_rows(payload('2026-11-05T09:00:00+08:00',close=100),symbol),cal)
    with pytest.raises(ValueError,match='GAP'):merge_same_source(bars,tq_rows(payload('2026-11-05T09:02:00+08:00'),symbol),cal)


def test_five_ready_cannot_make_ten_product_service_ready(tmp_path):
    from test_paper_service_health import runner
    # Existing production-shaped five-product fixture must explicitly expand both
    # pool and calendars; five healthy children cannot satisfy a ten-product gate.
    r,t=runner(tmp_path);r.c['universe']=list(TEN_POOL)
    assert not r.service_health.ready_facts(NOW)


@pytest.mark.parametrize('case',['valid','pause','missing_feed','dead_feed','stale','generation','profile','mapping','gm_extra'])
def test_ten_product_service_requires_each_real_source_without_gm_extra_subscriptions(tmp_path,case):
    from test_paper_service_health import runner,SYMBOLS as GM_SYMBOLS
    from test_paper_runtime_health_v6 import setup_health
    from futures_trend_quant.live.paper_service_health import POLICY as SERVICE_POLICY,require_service
    r,t=runner(tmp_path,at=NOW)
    r.c.update(root=str(tmp_path),universe=list(TEN_POOL),pool_policy_version=TEN_POOL_VERSION,external_market_policy=POLICY)
    setup_health(r.c);r.run_id='CURRENT'
    for symbol in SYMBOLS:
        r.calendar.register(symbol,r.engine.calendar)
        r.records[symbol]=dict(trade_windows=[])
        r.products[symbol]=dict(ready=True,frozen=False,fault=None)
    contracts={product_of(s):s for s in SYMBOLS}
    proof=dict(policy=POLICY,config_sha256=r.c['config_sha256'],run_id=r.run_id,status='RUNNING',updated_at=str(NOW),contracts=contracts,main_mapping=contracts)
    r.tq_feed=SimpleNamespace(is_alive=lambda:case!='dead_feed')
    if case=='missing_feed':r.tq_feed=None
    if case=='stale':proof['updated_at']=str(NOW-pd.Timedelta(seconds=4))
    if case=='generation':proof['run_id']='OLD'
    if case=='profile':proof['config_sha256']='OLD'
    if case=='mapping':proof['main_mapping']={}
    if case=='gm_extra':r.tick_subscriptions.update(SYMBOLS)
    if case=='pause':r.engine.s['pause_new']=True
    atomic(r.evidence/'run_manifest.json',dict(run_id=r.run_id,config_sha256=r.c['config_sha256'],service_policy_version=SERVICE_POLICY))
    append(r.evidence/'tq_feed'/r.run_id/'health.jsonl',proof)
    r.service_health.publish(NOW)
    assert r.service_health.state['service_facts_ready']==(case in ('valid','pause'))
    assert r.service_health.state['entry_gate_ready']==(case=='valid')
    if case=='valid':assert require_service(r.c,NOW)['entry_gate_ready'] is True
    else:
        with pytest.raises(ValueError,match='SERVICE_RECOVERY_PENDING'):require_service(r.c,NOW)
    assert not t.jobs


def test_ten_pool_requires_explicit_external_source_profile():
    with pytest.raises(ValueError,match='TQ_NATIVE_SOURCE_POLICY_REQUIRED'):validate_config(dict(pool_policy_version=TEN_POOL_VERSION),Path('.'))


def test_cold_tq_checkpoint_restores_exact_prefix_without_renewing_evidence(tmp_path):
    from futures_trend_quant.live.paper_data import SignalWorker
    c=dict(state_dir=str(tmp_path),external_market_policy=POLICY)
    w=SignalWorker(c,child=True);w.last=NOW
    w.seed_cache=dict(path='SYNTHETIC_IMMUTABLE_SEED',sha256='FROZEN',symbol=SYMBOLS[0],cutoff=str(NOW))
    w.runtime.used_evidence_ids.add('CONSUMED')
    w.runtime.evidence_clocks['CZCE.MA|E']=dict(first_eligible_at=str(NOW),expires_at=str(NOW+pd.Timedelta(seconds=30)))
    before=w.runtime.to_dict();w._checkpoint()
    restored=SignalWorker(c,child=True)
    assert restored.last==NOW and restored.runtime.to_dict()==before and restored.seed_cache==w.seed_cache
    assert restored.progress['warmup'] is False
