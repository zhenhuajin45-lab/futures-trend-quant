from datetime import timedelta
from pathlib import Path
import hashlib,json
import pandas as pd
import pytest
from test_paper_notice_details import fixture,NOW,put
from tools.paper_notice_details import LocalBars,M5DivergenceMonitor
from tools.paper_trend_notify import trend_risk,priority_divergences


def test_v5_selected_b_is_not_mislabelled_by_background_macd():
    f=dict(entry_policy_version='PAPER_ENTRY_V5',trade_permission=dict(new_entry_allowed=False,reasons=['M15_MACD_NOT_ALIGNED']),entry_permission=dict(new_entry_allowed=True),entry_policy_proof=dict(path='B'),top_div=True)
    text=trend_risk(dict(direction='bull',evidence=f,entry=dict(path='B',accepted=True)))
    assert 'B' in text and '仅提示' in text and '禁止新开' not in text and '历史' not in text
    f['entry_permission']=None;f['candidate_audit']=[]
    assert '无合格入场' in trend_risk(dict(direction='bull',evidence=f))
    assert '均线反侧' not in trend_risk(dict(direction='none',evidence=f))


def test_loader_uses_checkpoint_seed_and_keeps_volume_without_hiding_price_conflict(tmp_path):
    root,row=fixture(tmp_path);folder=root/'data/cache/paper_market/DCE_M_OLD';folder.mkdir(parents=True)
    bar=dict(symbol='DCE.m2701',frequency='60s',bob=(NOW-timedelta(minutes=1,seconds=10)).isoformat(),eob=(NOW-timedelta(seconds=10)).isoformat(),open=100,high=101,low=99,close=100,volume=10)
    seed=folder/'initial_m1.parquet';pd.DataFrame([bar]).to_parquet(seed)
    put(folder/'input_manifest.json',dict(symbol=bar['symbol'],requested_at=NOW.isoformat(),sha256=hashlib.sha256(seed.read_bytes()).hexdigest()))
    cp=root/'state/paper_observation_five/products/DCE_M/core_checkpoint.json';data=json.loads(cp.read_text());data['seed_cache']=dict(path=str(seed),symbol=bar['symbol'],cutoff=NOW.isoformat(),sha256=hashlib.sha256(seed.read_bytes()).hexdigest());put(cp,data)
    newer=root/'data/cache/paper_market/DCE_M_ZZZ';newer.mkdir();pd.DataFrame([dict(bar,close=100.5)]).to_parquet(newer/'initial_m1.parquet');put(newer/'input_manifest.json',dict(symbol=bar['symbol'],requested_at=NOW.isoformat(),sha256='0'*64))
    (newer/'input_manifest.json').write_text('unrelated corrupt later manifest')
    observed=root/'evidence/paper_observation_five/products/DCE_M/m1_observed.jsonl';observed.parent.mkdir(parents=True)
    observed.write_text(json.dumps(dict(bar,volume=20,local_observed_at=NOW.isoformat()))+'\n')
    loader=LocalBars(root);bars=loader.load('DCE.M',bar['symbol'],NOW)
    assert len(bars)==1 and bars[0].volume==10 and len(loader.volume_revisions)==1
    observed.write_text(json.dumps(dict(bar,close=100.5,local_observed_at=NOW.isoformat()))+'\n')
    with pytest.raises(ValueError,match='M1_OHLC_REVISION'):loader.load('DCE.M',bar['symbol'],NOW)


def minutes():
    from futures_trend_quant.core.models import Bar
    end=NOW-timedelta(seconds=10)
    return [Bar('DCE.m2701','60s',end-timedelta(minutes=250-i),end-timedelta(minutes=249-i),100,101,99,100) for i in range(250)]


def test_incomplete_m5_retries_in_same_minute_then_deduplicates(tmp_path,monkeypatch):
    root,row=fixture(tmp_path);monitor=M5DivergenceMonitor(tmp_path);bars=minutes();calls=[]
    monkeypatch.setattr(monitor.feed,'load',lambda *a:bars[:-1] if not calls else bars)
    from futures_trend_quant.core.entry.models import EvidenceEvent
    import futures_trend_quant.core.entry.evidence as evidence
    monkeypatch.setattr(evidence,'_latest_divergence',lambda b,i,direction,mode:EvidenceEvent('m5_top_divergence','bear',b[-1].identity,len(b)-1,b[-1].eob) if direction=='bear' else None)
    put(root/'evidence/paper_observation_five/m5_divergence_cursor.local.json',dict(cutoff=(NOW-timedelta(minutes=5)).isoformat(),processed=[],last_checked={}))
    assert monitor.poll(NOW)==[];calls.append(1)
    assert len(monitor.poll(NOW+timedelta(seconds=3)))==1
    assert monitor.poll(NOW+timedelta(seconds=6))==[]


def test_one_product_bad_source_does_not_hide_other_product_event(tmp_path,monkeypatch):
    root,row=fixture(tmp_path);ev=root/'evidence/paper_observation_five';s=json.loads((ev/'status.json').read_text());s['products']['DCE.Y']=dict(symbol='DCE.y2701',ready=True);put(ev/'status.json',s)
    reg=root/'state/paper_observation_five/market_registry.local.json';d=json.loads(reg.read_text());d['contracts']['DCE.y2701']=dict(d['contracts']['DCE.m2701']);put(reg,d)
    put(root/'state/paper_observation_five/products/DCE_Y/core_checkpoint.json',dict(last_eob=row['bar_eob'],runtime=dict(trends={'DCE.Y':dict(trend_state='bull')})))
    monitor=M5DivergenceMonitor(tmp_path)
    def load(cs,*args):
        if cs=='DCE.M':raise ValueError('M1_OHLC_REVISION')
        from dataclasses import replace
        return [replace(b,symbol='DCE.y2701') for b in minutes()]
    monkeypatch.setattr(monitor.feed,'load',load)
    from futures_trend_quant.core.entry.models import EvidenceEvent
    import futures_trend_quant.core.entry.evidence as evidence
    monkeypatch.setattr(evidence,'_latest_divergence',lambda b,i,direction,mode:EvidenceEvent('m5_top_divergence','bear',b[-1].identity,len(b)-1,b[-1].eob) if direction=='bear' else None)
    put(ev/'m5_divergence_cursor.local.json',dict(cutoff=(NOW-timedelta(minutes=5)).isoformat(),processed=[],last_checked={}))
    before=(root/'state/paper_observation_five/execution.json').read_bytes()
    assert len(monitor.poll(NOW))==1
    health=json.loads((ev/'m5_divergence_health.local.json').read_text());assert health['status']=='DEGRADED' and 'DCE.M' in health['errors']
    assert (root/'state/paper_observation_five/execution.json').read_bytes()==before


def test_m15_only_new_confirmation_once_and_no_restart_replay(tmp_path):
    root,row=fixture(tmp_path);ev=root/'evidence/paper_observation_five';confirmed=row['bar_eob'];event=dict(kind='top',mode='B_hist',previous_bar_id='DCE.m2701|900s|2026-11-05T20:45:00+08:00',current_bar_id='DCE.m2701|900s|2026-11-05T21:15:00+08:00',defined_at_bar_id='DCE.m2701|900s|'+confirmed,extreme_price=101)
    row['facts']['top_divergence_event']=event;(ev/'signals.jsonl').write_text(json.dumps(row)+'\n')
    path=ev/'m15_divergence_notice_cursor.local.json';put(path,dict(cutoff=(NOW-timedelta(minutes=5)).isoformat(),processed=[]))
    assert len(priority_divergences(tmp_path,NOW))==1 and priority_divergences(tmp_path,NOW)==[]
    put(path,dict(cutoff=(NOW-timedelta(minutes=5)).isoformat(),processed=[]));put(ev/'run_manifest.json',dict(started_at=NOW.isoformat()))
    assert priority_divergences(tmp_path,NOW)==[]


def test_new_m5_event_is_visible_even_without_m15_authorization(tmp_path,monkeypatch):
    root,row=fixture(tmp_path);cp=root/'state/paper_observation_five/products/DCE_M/core_checkpoint.json';d=json.loads(cp.read_text());d['runtime']['trends']['DCE.M']['trend_state']='none';put(cp,d)
    monitor=M5DivergenceMonitor(tmp_path);monkeypatch.setattr(monitor.feed,'load',lambda *a:minutes())
    from futures_trend_quant.core.entry.models import EvidenceEvent
    import futures_trend_quant.core.entry.evidence as evidence
    monkeypatch.setattr(evidence,'_latest_divergence',lambda b,i,direction,mode:EvidenceEvent('m5_top_divergence','bear',b[-1].identity,len(b)-1,b[-1].eob) if direction=='bear' else None)
    ev=root/'evidence/paper_observation_five';put(ev/'m5_divergence_cursor.local.json',dict(cutoff=(NOW-timedelta(minutes=5)).isoformat(),processed=[],last_checked={}))
    sent=monitor.poll(NOW);assert len(sent)==1
    card=json.loads((ev/'notifications'/f'{sent[0]}.json').read_text())['text']['card']
    assert '方向未授权' in card['header']['title']['content']
    assert '不是开仓或平仓确认' in json.dumps(card,ensure_ascii=False)
