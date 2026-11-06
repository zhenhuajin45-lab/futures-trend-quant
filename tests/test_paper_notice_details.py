from datetime import datetime,timedelta
import json
from pathlib import Path
import pytest
from tools.paper_notice_details import entry_plan,candidate_details,M5DivergenceMonitor
from tools.paper_trend_notify import ema_description

NOW=datetime.fromisoformat('2026-11-05T21:45:10+08:00')

def put(p,value):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value),encoding='utf8')

def fixture(tmp_path,direction='bull'):
    root=tmp_path/'_strategy_runtime'
    put(root/'config/paper_observation_five.local.json',dict(base_strategy='config/base.json',signal_max_age_seconds=30,quote_max_age_seconds=3,max_spread_ticks=4,open_wait_seconds=10))
    put(root/'config/base.json',dict(rules=dict(stop_buffer_atr=.6,stop_min_atr=1.5,stop_max_atr=6,div_mode='B_hist')))
    put(root/'config/paper_feishu.local.json',dict(enabled=False))
    put(root/'state/paper_observation_five/market_registry.local.json',dict(contracts={'DCE.m2701':dict(tick=1,multiplier=10,entry_windows=[(NOW.timestamp()-3600,NOW.timestamp()+3600)],trade_windows=[(NOW.timestamp()-20000,NOW.timestamp()+3600)])}))
    put(root/'state/paper_observation_five/execution.json',dict(intents={}))
    put(root/'state/paper_observation_five/products/DCE_M/core_checkpoint.json',dict(last_eob='2026-11-05T21:45:00+08:00',runtime=dict(anchors={'DCE.M|'+direction:dict(anchor_price=99 if direction=='bull' else 101)},trends={'DCE.M':dict(trend_state='bull')})))
    put(root/'evidence/paper_observation_five/status.json',dict(at=NOW.isoformat(),products={'DCE.M':dict(symbol='DCE.m2701',ready=True,name='豆粕')}))
    row=dict(csymbol='DCE.M',bar_eob='2026-11-05T21:45:00+08:00',signal_ready_at='2026-11-05T21:45:04+08:00',order_evaluated_at='2026-11-05T21:45:04+08:00',facts=dict(symbol='DCE.m2701',close=100,atr14=2),entry=dict(accepted=True,direction=direction,evidence_id='B|x',path='B',reason='PATH_B_ACCEPTED'),paper_decision=[False,'BROKER_BUSY'])
    return root,row


@pytest.mark.parametrize('direction',['bull','bear'])
def test_projected_plan_uses_exact_risk_function_and_original_expiry(tmp_path,direction):
    root,row=fixture(tmp_path,direction);p=entry_plan(root,row,NOW)
    from futures_trend_quant.core.risk import build_stop_plan
    exact=build_stop_plan(direction=direction,entry=100,anchor=99 if direction=='bull' else 101,atr=2,tick=1)
    assert (p['stop'],p['target'],p['risk_yuan'])==(exact.stop,exact.target,exact.distance*10)
    assert p['remaining_seconds']==20 and p['max_age_seconds']==30 and p['block']=='BROKER_BUSY'
    assert p['basis']=='SIGNAL_CLOSE_PROJECTION' and not p['allowed']


def test_only_quote_known_at_actual_evaluation_can_supply_entry_price(tmp_path):
    root,row=fixture(tmp_path)
    quote=lambda t,bid,ask:dict(symbol='DCE.m2701',local_received_at=t,quote=dict(created_at=t,bid=bid,ask=ask,bid_size=10,ask_size=10))
    q=[quote('2026-11-05T21:45:03+08:00',100,101),quote('2026-11-05T21:45:05+08:00',105,106)]
    p=entry_plan(root,row,NOW,q);assert p['entry']==101 and p['basis']=='OBSERVED_OPPONENT_QUOTE_PROJECTION'


def test_actual_intent_is_used_even_after_checkpoint_moves(tmp_path):
    root,row=fixture(tmp_path)
    put(root/'state/paper_observation_five/execution.json',dict(intents={'i':dict(evidence_id='DCE.M|B|x',symbol='DCE.m2701',effect=1,signal_at=row['bar_eob'],price=101,stop=98,target=107,multiplier=10,filled=1,status=3)}))
    p=entry_plan(root,row,NOW);assert p['basis']=='ACTUAL_INTENT' and p['entry']==101 and p['filled']==1


def test_same_evidence_old_intent_does_not_become_current_candidate_order(tmp_path):
    root,row=fixture(tmp_path)
    put(root/'state/paper_observation_five/execution.json',dict(intents={'i':dict(evidence_id='DCE.M|B|x',symbol='DCE.m2701',effect=1,signal_at='2026-11-05T21:30:00+08:00',price=500,stop=490,target=520,multiplier=10)}))
    p=entry_plan(root,row,NOW);assert p['basis']=='SIGNAL_CLOSE_PROJECTION' and p['entry']==100 and not p['allowed']


def test_same_evidence_new_m15_evaluation_has_its_own_expiry_card(tmp_path):
    root,row=fixture(tmp_path);candidate_details(tmp_path,NOW-timedelta(seconds=10));ev=root/'evidence/paper_observation_five'
    (ev/'signals.jsonl').write_text(json.dumps(row)+'\n',encoding='utf8');assert len(candidate_details(tmp_path,NOW))==1
    row['bar_eob']='2026-11-05T22:00:00+08:00';row['signal_ready_at']='2026-11-05T22:00:04+08:00';row['order_evaluated_at']=row['signal_ready_at']
    with (ev/'signals.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(row)+'\n')
    assert len(candidate_details(tmp_path,NOW+timedelta(minutes=15)))==1
    assert candidate_details(tmp_path,NOW+timedelta(minutes=15))==[]


def test_v2_expired_startup_candidate_is_local_evidence_only(tmp_path):
    root,row=fixture(tmp_path);candidate_details(tmp_path,NOW-timedelta(seconds=10));ev=root/'evidence/paper_observation_five'
    row['facts']['evidence_clock']=dict(first_eligible_at='2026-11-05T21:15:00+08:00',expires_at='2026-11-05T21:15:30+08:00')
    (ev/'signals.jsonl').write_text(json.dumps(row)+'\n',encoding='utf8')
    assert candidate_details(tmp_path,NOW)==[]
    assert not list((ev/'notifications').glob('*.json'))


def test_old_candidate_never_uses_new_anchor_and_invalid_stop_is_not_invented(tmp_path):
    root,row=fixture(tmp_path);p=root/'state/paper_observation_five/products/DCE_M/core_checkpoint.json';s=json.loads(p.read_text());s['last_eob']='2026-11-05T22:00:00+08:00';put(p,s)
    result=entry_plan(root,row,NOW);assert result['invalid']=='ANCHOR_NOT_SAME_SIGNAL_BAR' and result['stop'] is None
    s['last_eob']=row['bar_eob'];s['runtime']['anchors']['DCE.M|bull']['anchor_price']=1;put(p,s)
    result=entry_plan(root,row,NOW);assert result['invalid']=='STRUCTURAL_STOP_INVALID' and result['target'] is None


def test_candidate_detail_delivery_has_plan_expiry_block_and_dedup_without_trade_mutation(tmp_path):
    root,row=fixture(tmp_path);assert candidate_details(tmp_path,NOW-timedelta(seconds=10))==[]
    ev=root/'evidence/paper_observation_five';(ev/'signals.jsonl').write_text(json.dumps(row)+'\n',encoding='utf8')
    before={p:p.read_bytes() for p in (root/'state').rglob('*.json')}
    result=candidate_details(tmp_path,NOW);assert len(result)==1 and candidate_details(tmp_path,NOW)==[]
    card=json.loads((ev/'notifications'/f'{result[0]}.json').read_text(encoding='utf8'))['text']['card'];body=json.dumps(card,ensure_ascii=False)
    assert all(x in body for x in ['止损价','2R目标价','30秒','0.5分钟','BROKER_BUSY','不自动补单','参考计算'])
    assert all(p.read_bytes()==data for p,data in before.items())


def test_risk_budget_warning_is_current_reference_not_historical_order_approval(tmp_path):
    root,row=fixture(tmp_path);p=root/'config/paper_observation_five.local.json';config=json.loads(p.read_text());config['risk_pct']=.0025;put(p,config)
    p=root/'evidence/paper_observation_five/status.json';s=json.loads(p.read_text());s['broker']=dict(nav=10000);put(p,s)
    result=entry_plan(root,row,NOW)
    assert result['current_risk_budget_yuan']==25 and result['risk_yuan']==30 and not result['within_current_one_lot_risk']
    assert result['block']=='BROKER_BUSY' and not result['allowed']


def test_wrong_side_anchor_levels_are_not_presented_as_a_valid_trade_plan(tmp_path):
    root,row=fixture(tmp_path);p=root/'state/paper_observation_five/products/DCE_M/core_checkpoint.json';s=read_json(p);s['runtime']['anchors']['DCE.M|bull']['anchor_price']=110;put(p,s)
    row['facts']['top_div']=True;ev=root/'evidence/paper_observation_five';candidate_details(tmp_path,NOW-timedelta(seconds=10));(ev/'signals.jsonl').write_text(json.dumps(row)+'\n',encoding='utf8')
    result=candidate_details(tmp_path,NOW);card=read_json(ev/'notifications'/f'{result[0]}.json')['text']['card'];fields={f['text']['content'].split('\n',1)[0].strip('*'):f['text']['content'].split('\n',1)[1] for e in card['elements'] for f in e.get('fields',[])}
    assert fields['止损价']=='不可用：结构依据异常' and fields['2R目标价']=='不可用：结构依据异常'
    assert '勿据此手工下单' in fields['当前有效性'] and '已有M15顶背驰' in json.dumps(card,ensure_ascii=False)


def test_display_rounding_does_not_change_cross_detection():
    text=ema_description(dict(close=100.004,ema34=100.003,slope_state='up'),dict(close=99,ema34=100))
    assert 'EMA34 100.00' in text and '均线上方' in text and '上穿' in text and '均线上行' in text
    assert '下穿' in ema_description(dict(close=99,ema34=100,slope_state='down'),dict(close=101,ema34=100))
    assert '上穿' not in ema_description(dict(close=101,ema34=100,slope_state='flat'))


def test_m5_alert_uses_original_closed_m5_detector_only_once(tmp_path,monkeypatch):
    root,row=fixture(tmp_path);monitor=M5DivergenceMonitor(tmp_path)
    from futures_trend_quant.core.models import Bar
    from futures_trend_quant.core.entry.models import EvidenceEvent
    import futures_trend_quant.core.entry.evidence as evidence
    bars=[Bar('DCE.m2701','60s',NOW-timedelta(minutes=250-i,seconds=10),NOW-timedelta(minutes=249-i,seconds=10),100,101,99,100) for i in range(250)]
    # Last completed bar ends at the current five-minute boundary; future bars absent.
    monkeypatch.setattr(monitor.feed,'load',lambda *a:bars)
    calls=[]
    def detect(series,indicators,direction,mode):
        calls.append((series[-1].frequency,series[-1].eob,direction,mode))
        return EvidenceEvent('m5_top_divergence','bear',series[-1].identity,len(series)-1,series[-1].eob) if direction=='bear' else None
    monkeypatch.setattr(evidence,'_latest_divergence',detect)
    put(root/'evidence/paper_observation_five/m5_divergence_cursor.local.json',dict(cutoff=(NOW-timedelta(minutes=5)).isoformat(),processed=[],last_checked={}))
    before=(root/'state/paper_observation_five/execution.json').read_bytes()
    result=monitor.poll(NOW);assert len(result)==1 and monitor.poll(NOW+timedelta(seconds=1))==[]
    assert all(frequency=='300s' and eob<=NOW and mode=='B_hist' for frequency,eob,direction,mode in calls)
    assert (root/'state/paper_observation_five/execution.json').read_bytes()==before


def test_no_divergence_or_incomplete_m5_produces_no_alert(tmp_path,monkeypatch):
    root,row=fixture(tmp_path);monitor=M5DivergenceMonitor(tmp_path)
    from futures_trend_quant.core.models import Bar
    bars=[Bar('DCE.m2701','60s',NOW-timedelta(minutes=250-i,seconds=10),NOW-timedelta(minutes=249-i,seconds=10),100,101,99,100) for i in range(250)]
    monkeypatch.setattr(monitor.feed,'load',lambda *a:bars)
    assert monitor.poll(NOW)==[]
    assert not list((root/'evidence/paper_observation_five/notifications').glob('*.json'))
    monitor.last_minute=None;cursor=root/'evidence/paper_observation_five/m5_divergence_cursor.local.json';put(cursor,dict(cutoff=(NOW-timedelta(minutes=5)).isoformat(),processed=[],last_checked={}))
    monkeypatch.setattr(monitor.feed,'load',lambda *a:bars[:-1])
    assert monitor.poll(NOW)==[] and read_json(cursor)['last_checked']=={}


def read_json(path):return json.loads(path.read_text(encoding='utf8'))


def test_local_m1_seed_is_hash_verified_before_reading_price(tmp_path):
    from tools.paper_notice_details import LocalBars
    root,row=fixture(tmp_path);folder=root/'data/cache/paper_market/DCE_M_20261105';folder.mkdir(parents=True)
    put(folder/'input_manifest.json',dict(symbol='DCE.m2701',requested_at=NOW.isoformat(),sha256='0'*64))
    (folder/'initial_m1.parquet').write_bytes(b'changed original')
    with pytest.raises(ValueError,match='WARMUP_HASH_MISMATCH'):LocalBars(root).load('DCE.M','DCE.m2701',NOW)
