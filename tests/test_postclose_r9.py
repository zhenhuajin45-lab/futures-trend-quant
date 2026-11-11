from pathlib import Path
from datetime import timedelta,datetime,timezone
from copy import deepcopy
import hashlib,json,queue
from types import SimpleNamespace
import pandas as pd
import pytest
from test_paper_notice_details import fixture,NOW,put
from test_paper_notice_realtime import minutes
from tools.paper_notice_details import LocalBars,M5DivergenceMonitor
from futures_trend_quant.live.paper_tq import POLICY

ADDED=[('CZCE.MA','CZCE.MA701'),('DCE.EG','DCE.eg2701'),('DCE.JD','DCE.jd2612'),('CZCE.CJ','CZCE.CJ701'),('SHFE.AG','SHFE.ag2702')]


def tq_fixture(tmp_path,cs,symbol):
    from dataclasses import asdict,replace
    root,row=fixture(tmp_path);config=root/'config/paper_observation_five.local.json'
    c=json.loads(config.read_text());c['external_market_policy']=POLICY;put(config,c)
    records=[asdict(replace(b,symbol=symbol)) for b in minutes()]
    folder=root/'data/cache/paper_market'/(cs.replace('.','_')+'_CANONICAL');folder.mkdir(parents=True)
    seed=folder/'initial_m1.parquet';pd.DataFrame(records[:-1]).to_parquet(seed)
    digest=hashlib.sha256(seed.read_bytes()).hexdigest()
    put(folder/'input_manifest.json',dict(source='TQ_CANONICAL_CONSUMED_PREFIX',symbol=symbol,sha256=digest))
    state=root/'state/paper_observation_five/products'/cs.replace('.','_');state.mkdir(parents=True,exist_ok=True)
    def serial(record):return {k:v.isoformat() if isinstance(v,datetime) else v for k,v in record.items()}
    journal=state/'tq_core_m1.jsonl';raw=(json.dumps(dict(policy=POLICY,csymbol=cs,bar=serial(records[-1]),
        source_known_at=(NOW-timedelta(seconds=3)).isoformat(),recorded_at=(NOW-timedelta(seconds=2)).isoformat()))+'\n').encode();journal.write_bytes(raw)
    cp=dict(last_eob=records[-1]['eob'].isoformat(),seed_cache=dict(path=str(seed),sha256=digest,symbol=symbol,cutoff=(NOW-timedelta(minutes=1)).isoformat()),
        tq_input_prefix=dict(path=str(journal.resolve()),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()),runtime=dict(trends={cs:dict(trend_state='bull')}))
    put(state/'core_checkpoint.json',cp)
    # These actual legacy shapes caused R9's regression; neither is TQ input.
    log=root/'evidence/paper_observation_five/products'/cs.replace('.','_')/'m1_observed.jsonl';log.parent.mkdir(parents=True,exist_ok=True)
    log.write_text(json.dumps(dict(serial(records[0]),close=100.5,local_observed_at=NOW.isoformat()))+'\n')
    extra=root/'data/cache/paper_market'/(cs.replace('.','_')+'_TQ_CURRENT');extra.mkdir()
    put(extra/'input_manifest.json',dict(source='TQ_DOWNLOADER_AND_TQ_LIVE_SNAPSHOT',symbol=symbol,known_at=NOW.isoformat()))
    return root,records,state


@pytest.mark.parametrize('cs,symbol',ADDED)
def test_tq_actual_notice_consumer_ignores_retired_gm_and_requested_at_manifest(tmp_path,cs,symbol):
    root,records,state=tq_fixture(tmp_path,cs,symbol);before=(root/'state/paper_observation_five/execution.json').read_bytes()
    loader=LocalBars(root);bars=loader.load(cs,symbol,NOW)
    assert len(bars)==len(records) and bars[0].close==100 and bars[-1].eob==records[-1]['eob']
    assert loader.sources[cs][-1]['role']=='TQ_CORE_COMMITTED_INPUT'
    assert before==(root/'state/paper_observation_five/execution.json').read_bytes()
    journal=state/'tq_core_m1.jsonl';changed=json.loads(journal.read_text());changed['bar']['close']=100.5
    journal.write_text(json.dumps(changed)+'\n')
    with pytest.raises(ValueError,match='PREFIX_CHANGED'):loader.load(cs,symbol,NOW)


def test_tq_notice_asof_does_not_use_later_acceptance(tmp_path):
    cs,symbol=ADDED[0];root,records,state=tq_fixture(tmp_path,cs,symbol)
    path=state/'tq_core_m1.jsonl';r=json.loads(path.read_text());r['recorded_at']=(NOW+timedelta(seconds=1)).isoformat();raw=(json.dumps(r)+'\n').encode();path.write_bytes(raw)
    cp=json.loads((state/'core_checkpoint.json').read_text());cp['last_eob']=(NOW-timedelta(minutes=15)).isoformat();cp['tq_input_prefix'].update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest());put(state/'core_checkpoint.json',cp)
    assert len(LocalBars(root).load(cs,symbol,NOW))==len(records)-1


def test_tq_monitor_migrates_legacy_cursor_but_preserves_old_error_and_dedupe(tmp_path,monkeypatch):
    cs,symbol=ADDED[0];root,records,state=tq_fixture(tmp_path,cs,symbol);ev=root/'evidence/paper_observation_five'
    put(ev/'status.json',dict(at=NOW.isoformat(),products={cs:dict(symbol=symbol,ready=True)}))
    registry=json.loads((root/'state/paper_observation_five/market_registry.local.json').read_text());registry['contracts'][symbol]=registry['contracts']['DCE.m2701'];put(root/'state/paper_observation_five/market_registry.local.json',registry)
    cursor=ev/'m5_divergence_cursor.local.json';put(cursor,dict(cutoff=(NOW-timedelta(minutes=5)).isoformat(),processed=['KEEP_OLD_ID'],last_checked={cs:NOW.timestamp()+86400},errors={cs:dict(reason='KeyError')}))
    from futures_trend_quant.core.entry.models import EvidenceEvent
    import futures_trend_quant.core.entry.evidence as evidence
    monkeypatch.setattr(evidence,'_latest_divergence',lambda bars,ind,direction,mode:EvidenceEvent('m5_top_divergence','bear',bars[-1].identity,len(bars)-1,bars[-1].eob) if direction=='bear' else None)
    monitor=M5DivergenceMonitor(tmp_path);before=(root/'state/paper_observation_five/execution.json').read_bytes()
    assert len(monitor.poll(NOW))==1
    saved=json.loads(cursor.read_text());assert 'KEEP_OLD_ID' in saved['processed']
    assert saved['source_migrations'][0]['previous_error']['reason']=='KeyError'
    assert saved['last_checked'][cs]<=NOW.timestamp() and cs not in saved['errors']
    assert monitor.poll(NOW)==[]
    assert before==(root/'state/paper_observation_five/execution.json').read_bytes()


def test_per_product_progress_is_real_and_hung_product_still_fails_30_seconds(tmp_path,monkeypatch):
    root,_=fixture(tmp_path);ev=root/'evidence/paper_observation_five';status=json.loads((ev/'status.json').read_text());status['products']={cs:dict(symbol=symbol,ready=True) for cs,symbol in ADDED};put(ev/'status.json',status)
    from futures_trend_quant.live.paper_progress import TaskProgress,progress_problem
    progress=TaskProgress(tmp_path/'progress.json','R9');monitor=M5DivergenceMonitor(tmp_path);seen=[]
    def poll(now,csymbol=None):
        assert progress.body['stage']=='M5_DIVERGENCE:'+csymbol and progress.body['status']=='RUNNING'
        age=datetime.fromisoformat(progress.body['stage_started_at'])+timedelta(seconds=31)
        assert 'PROGRESS_STALE' in progress_problem(dict(progress.body,at=age.isoformat()),progress.body['pid'],age)
        seen.append(csymbol);return []
    monkeypatch.setattr(monitor,'poll',poll);monitor.poll_progress(progress,lambda:NOW)
    assert seen==list(status['products']) and progress.body['sequence']==5 and progress.body['status']=='COMPLETED'


def test_actual_cycle_returns_from_quote_backlog_and_applies_reply_before_risk(tmp_path,monkeypatch):
    from test_paper_pool_rotation import cycle_fixture
    from futures_trend_quant.live import paper_five,paper_cycle_work
    r=cycle_fixture(tmp_path);clock=[0.];work=paper_cycle_work.CycleWork(lambda:clock[0]);monkeypatch.setattr(paper_cycle_work,'CycleWork',lambda:work)
    r.evidence=tmp_path;r.quote_events=queue.Queue();r.host_ticks=queue.Queue();r.restore_strategy_fills=lambda:None
    for cs,p in r.products.items():p['market'].events=queue.Queue()
    for n in range(500):r.quote_events.put(('QUOTE','DCE.m2701',dict(last=n),'KNOWN'))
    seen=[];replies=[]
    def accept(symbol,q,received):
        seen.append(q['last']);clock[0]+=.02
        if len(seen)==1:replies.append((dict(op='snapshot'),dict(ok=True)))
    r.accept_quote=accept;r.engine.accept=lambda j,a,t:seen.append('APPLIED')
    def drain():out=list(replies);replies.clear();return out
    r.transport.drain=drain;r.market_events=lambda now:paper_five.FiveRunner.market_events(r,now)
    r.engine.tick=lambda now:seen.append('RISK')
    assert r.cycle() is False
    assert 0<sum(isinstance(x,int) for x in seen)<500 and r.quote_events.qsize()>0
    assert seen.index('APPLIED')<seen.index('RISK')
    assert work.snapshot()['queues']['QUOTE_POLL']['deferred']


def test_calendar_golden_friday_holiday_and_exact_section_identity():
    from futures_trend_quant.live.paper_calendar import PaperCalendar
    c=PaperCalendar(['2026-09-30','2026-10-09','2026-10-12','2026-10-13'],[dict(start='21:00',end='2:30'),dict(start='9:00',end='10:15')],'SHFE.AG')
    assert not c.market_open('2026-09-30T21:30:00+08:00')
    assert c.market_open('2026-10-10T01:00:00+08:00') and c.day('2026-10-10T01:00:00+08:00')=='2026-10-12'
    assert c.daily_counts['2026-10-12']==405
    from futures_trend_quant.core.models import Bar
    a=datetime.fromisoformat('2026-10-09T21:00:00+08:00');b=a+timedelta(minutes=15)
    assert c.section_for_bar(Bar('SHFE.ag2702','900s',a,b,100,101,99,100))=='SHFE.AG|2026-10-12|2026-10-09T21:00:00+08:00|2026-10-10T02:30:00+08:00'
