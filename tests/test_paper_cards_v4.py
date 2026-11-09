from copy import deepcopy
from datetime import timedelta,datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from collections import Counter
import json,queue
import pytest
from test_paper_execution import engine,signal,quote,NOW
from futures_trend_quant.live.paper_cards import POLICY,freeze_plan,candidate_card,intent_plan,fill_protection
from futures_trend_quant.live.paper_notify import FeishuOutbox,expire_before_send,message,send_once


def fields(card):
    return {f['text']['content'].split('\n',1)[0].strip('*'):f['text']['content'].split('\n',1)[1]
            for el in card['card']['elements'] for f in el.get('fields',[])}


def plan(tmp_path,**patch):
    e,t=engine(tmp_path);s=signal();s.update(first_eligible_at=NOW.isoformat(),expires_at=(NOW+timedelta(seconds=30)).isoformat(),tick=1.)
    s.update(patch)
    return freeze_plan(s,100,e.quotes['DCE.m2701'],'OK',NOW,e.c,1000000),e


@pytest.mark.parametrize('direction,anchor,stop,target',[('bull',99.,97.,109.),('bear',103.,105.,90.)])
def test_frozen_candidate_has_exact_levels_risk_validity_and_readable_status(tmp_path,direction,anchor,stop,target):
    p,e=plan(tmp_path,direction=direction,anchor=anchor)
    card=candidate_card(p,(False,'BROKER_BUSY'),NOW+timedelta(seconds=4),'豆粕');f=fields(card)
    assert p['stop']==stop and p['target']==target
    assert f['止损价']==f'{stop:.2f}' and f['2R目标价']==f'{target:.2f}'
    assert '26秒' in f['信号有效期'] and '本地执行通道忙' in f['执行状态']
    assert card['card']['header']['title']['content'].count('截至 ')==1
    assert card['card']['header']['title']['content'].endswith('截至 2026-11-05  10:00:00')


@pytest.mark.parametrize('anchor,kind',[(110.,'INVALID_ANCHOR'),(1.,'STRUCTURAL_STOP_INVALID')])
def test_invalid_structure_is_block_card_not_an_entry_candidate(tmp_path,anchor,kind):
    p,e=plan(tmp_path,anchor=anchor);c=candidate_card(p,(False,'CANDIDATE_WAITING'),NOW,'豆粕');f=fields(c)
    assert not p['structural_valid'] and p['invalid']==kind
    assert '信号拦截' in c['card']['header']['title']['content'] and '入场候选' not in c['card']['header']['title']['content']
    assert f['止损价'].startswith('未生成') and f['2R目标价'].startswith('未生成')
    assert f['参考入场价']=='101.00' and f['拦截原因']
    if anchor==1.:assert '结构距离 / 允许上限' in f


def test_missing_quote_keeps_prices_explicit_reference_and_never_claims_executable(tmp_path):
    p,e=plan(tmp_path);s=signal();s['tick']=1.
    p=freeze_plan(s,100,None,'QUOTE_MISSING',NOW,e.c)
    c=candidate_card(p,(False,'CANDIDATE_WAITING'),NOW,'豆粕');f=fields(c)
    assert '信号观察·待报价' in c['card']['header']['title']['content']
    assert '仅参考计算' in f['价位依据'] and f['止损价']!='未取得'


def test_one_lot_over_budget_looks_blocked_even_with_valid_stop(tmp_path):
    p,e=plan(tmp_path);p.update(risk_budget_yuan=1,within_risk_budget=False)
    c=candidate_card(p,(False,'CANDIDATE_WAITING'),NOW)
    assert '信号拦截' in c['card']['header']['title']['content'] and '1手超预算' in fields(c)['单笔风险预算']


def runner(tmp_path,monkeypatch,*,anchor=99.,pause=False):
    import futures_trend_quant.live.paper_five as five
    from futures_trend_quant.core.models import Bar
    e,t=engine(tmp_path);e.s['pause_new']=pause
    s=signal();e.c.update(notification_policy_version=POLICY,base_config=SimpleNamespace(raw={'rules':s['rules']}))
    bar=Bar('DCE.m2701','900s',NOW-timedelta(minutes=15),NOW,100,102,98,100)
    permission=SimpleNamespace(new_entry_allowed=True,technical_exit_direction=None,policy='CLOSED_FACTS_PERMISSION_V1',background='bull')
    entry=SimpleNamespace(accepted=True,direction='bull',path='B',evidence_id='B|FROZEN',trigger_bar_id='M5_TRIGGER',reason='PATH_B_ACCEPTED')
    result=SimpleNamespace(entry=entry,permission=permission,gate=None,prior_trend='bull',trend_changed=False,m15_index=1234,
        trend=SimpleNamespace(confirmed=False,confirmed_state='bull',state=SimpleNamespace(since_bar_id='EPISODE')),
        anchor=SimpleNamespace(anchor_price=anchor,anchor_bar_id='DCE.m2701|900s|'+(NOW-timedelta(minutes=30)).isoformat(),
                               confirmed_at_bar_id='DCE.m2701|900s|'+(NOW-timedelta(minutes=15)).isoformat(),episode_id='EPISODE'),
        analysis=SimpleNamespace(bar=bar,indicators=SimpleNamespace(atr14=[2.]),finalized_h1_absolute='bull_strong',finalized_h1_eob=NOW),
        facts_row=dict(symbol='DCE.m2701',close=100,atr14=2,slope_state='up'),
        evidence_clock=dict(evidence_created_at=(NOW-timedelta(minutes=5)).isoformat(),first_eligible_at=NOW.isoformat(),expires_at=(NOW+timedelta(seconds=30)).isoformat()))
    out=queue.Queue();out.put(('CORE_RESULT',result,bar,NOW.isoformat()))
    r=five.FiveRunner.__new__(five.FiveRunner);r.engine=e;r.c=e.c;r.evidence=e.evidence;r.counts=Counter()
    r.products={'DCE.M':dict(core=SimpleNamespace(outbox=out),counts=Counter(),ready=True,frozen=False,coverage={'qualified':True},
                           metadata=dict(spec=dict(multiplier=10.,margin_ratio=.07,price_tick=1.)))}
    cfg=tmp_path/'notify.json';cfg.write_text('{"enabled":false}')
    r.notifications=FeishuOutbox(cfg,e.evidence,start=False);r.order_offset=0;r.cursor_path=tmp_path/'cursor.json'
    monkeypatch.setattr(five,'pd',SimpleNamespace(Timestamp=SimpleNamespace(now=lambda **k:NOW)))
    return r,e,t


def test_actual_coordinator_freezes_same_signal_and_queues_one_card_without_order(tmp_path,monkeypatch):
    r,e,t=runner(tmp_path,monkeypatch);r.core_events(NOW)
    rows=[json.loads(x) for x in (e.evidence/'signals.jsonl').read_text(encoding='utf8').splitlines()]
    row=rows[-1];p=row['candidate_plan']
    assert row['notice_schema']=='CANDIDATE_CARD_V4' and p['stop']==97 and p['target']==109 and p['anchor']==99
    assert len(list(r.notifications.root.glob('*.json')))==1 and not t.jobs
    assert e.pending and not e.s['intents']
    saved=json.loads(next(r.notifications.root.glob('*.json')).read_text(encoding='utf8'))
    assert saved['expires_at']==p['expires_at'] and fields(saved['text'])['止损价']=='97.00'
    # Queue drains only through the existing engine. The next message is the actual intent plan.
    e.drain_candidates(NOW);assert len(t.jobs)==1
    r.pump_notifications()
    cards=[json.loads(p.read_text(encoding='utf8'))['text'] for p in r.notifications.root.glob('*.json')]
    assert sum('入场候选' in c['card']['header']['title']['content'] for c in cards)==1
    assert sum('本地入场意图·待执行回执' in c['card']['header']['title']['content'] for c in cards)==1


def test_paused_real_coordinator_reports_known_pause_not_only_waiting(tmp_path,monkeypatch):
    r,e,t=runner(tmp_path,monkeypatch,pause=True);r.core_events(NOW)
    card=json.loads(next(r.notifications.root.glob('*.json')).read_text(encoding='utf8'))['text']
    assert '信号拦截' in card['card']['header']['title']['content']
    assert '已停开' in fields(card)['执行状态'] and not t.jobs


def test_frozen_plan_survives_later_checkpoint_and_notifier_does_not_double_send(tmp_path):
    from test_paper_notice_details import fixture,put,NOW as at
    from tools.paper_notice_details import entry_plan,candidate_details
    root,row=fixture(tmp_path);p,e=plan(tmp_path/'calc');p.update(first_eligible_at=row['bar_eob'],expires_at=(at+timedelta(seconds=20)).isoformat())
    row.update(candidate_plan=p,notice_schema='CANDIDATE_CARD_V4')
    put(root/'state/paper_observation_five/products/DCE_M/core_checkpoint.json',dict(last_eob='2099-01-01T00:00:00+08:00'))
    frozen=entry_plan(root,row,at);assert frozen['stop']==97 and frozen['target']==109
    candidate_details(tmp_path,at-timedelta(seconds=20))
    (root/'evidence/paper_observation_five/signals.jsonl').write_text(json.dumps(row)+'\n',encoding='utf8')
    assert candidate_details(tmp_path,at)==[]


def test_closed_trade_card_never_uses_new_positions_protection():
    i=dict(id='OLD',effect=1,direction='bull',stop=97.)
    newer=dict(open_intent_id='NEW',stop=200,target=300,deadline='FUTURE')
    p=fill_protection(i,newer,dict(price=102.))
    assert p['stop']==97 and p['target']==112 and p['deadline']=='未取得'
    close=dict(effect=2,initial_stop=97,initial_target=109,deadline='ORIGINAL')
    assert fill_protection(close,newer,dict(price=100))==dict(stop=97,target=109,deadline='ORIGINAL')


def test_expired_candidate_is_not_sent_or_retried_after_original_deadline(tmp_path):
    cfg=tmp_path/'notify.json';cfg.write_text('{"enabled":false}');box=FeishuOutbox(cfg,tmp_path,start=False)
    key=box.enqueue('EVENT',message('候选',[]),expires_at=NOW.isoformat(),priority=3)
    row=json.loads((box.root/(key+'.json')).read_text(encoding='utf8'));row['status']='RATE_LIMITED'
    assert expire_before_send(row,NOW.to_pydatetime()) and row['status']=='EXPIRED_NOT_SENT'
    assert row['attempts']==0
    durable=dict(status='PENDING',created_at=NOW.isoformat())
    assert not expire_before_send(durable,NOW.to_pydatetime()+timedelta(days=1))


def test_rate_limit_has_bounded_retry_category_unknown_delivery_stays_unknown():
    from test_paper_notify import Opener,Response,HOOK
    r=send_once(dict(webhook=HOOK),message('检查',[]),Opener(Response(b'{"code":11232,"msg":"rate limited"}')))
    assert r['status']=='RATE_LIMITED' and r['vendor_message']=='rate limited'
    assert send_once(dict(webhook=HOOK),'x',Opener(error=TimeoutError()))['status']=='UNKNOWN_DELIVERY'


def test_all_card_types_have_single_asof_and_valid_component_group_sizes():
    for title in ['委托已受理','入场已成交','离场已成交','退出待完成','日盘收盘账户摘要','🚨趋势反转｜截至 2026-11-06  10:00:00']:
        card=message(title,[],fields=[(str(n),str(n)) for n in range(21)])
        assert card['card']['header']['title']['content'].count('截至 ')==1
        assert max(len(e.get('fields',[])) for e in card['card']['elements'])<=10
        assert len(json.dumps(card,ensure_ascii=False).encode())<19000


def test_source_only_record_is_not_an_external_duplicate(tmp_path):
    cfg=tmp_path/'config.json';cfg.write_text('{"enabled":true,"webhook":"not-used"}')
    box=FeishuOutbox(cfg,tmp_path,start=False)
    key=box.enqueue('SOURCE',message('趋势方向变更',[]),deliver=False)
    assert json.loads((box.root/(key+'.json')).read_text(encoding='utf8'))['status']=='LOCAL_SOURCE_ONLY'


@pytest.mark.parametrize('case',['expired','payload','unknown','limited'])
def test_actual_sender_preserves_delivery_safety_and_stays_alive_on_bad_payload(tmp_path,monkeypatch,case):
    import futures_trend_quant.live.paper_notify as notify
    from test_paper_notify import HOOK
    from unittest.mock import Mock
    cfg=tmp_path/'notify.json';cfg.write_text(json.dumps(dict(enabled=True,webhook=HOOK)))
    box=FeishuOutbox(cfg,tmp_path,start=False)
    key=box.enqueue('EVENT',message('验收',[]),expires_at=NOW.isoformat() if case=='expired' else None)
    send=Mock(side_effect=ValueError('too large')) if case=='payload' else Mock(return_value=dict(status='UNKNOWN_DELIVERY' if case=='unknown' else 'RATE_LIMITED',vendor_code=11232))
    monkeypatch.setattr(notify,'send_once',send)
    def finish(_):raise RuntimeError('END_SINGLE_CYCLE')
    monkeypatch.setattr(notify.time,'sleep',finish)
    with pytest.raises(RuntimeError,match='END_SINGLE_CYCLE'):notify.sender(cfg,box.root)
    row=json.loads((box.root/(key+'.json')).read_text(encoding='utf8'))
    expected={'expired':'EXPIRED_NOT_SENT','payload':'LOCAL_REJECTED_PAYLOAD','unknown':'UNKNOWN_DELIVERY','limited':'RATE_LIMITED'}
    assert row['status']==expected[case]
    if case=='expired':send.assert_not_called()
    elif case=='limited':assert row['attempts']==1 and row['retry_at']>0
    else:
        send.assert_called_once();send.reset_mock()
        with pytest.raises(RuntimeError,match='END_SINGLE_CYCLE'):notify.sender(cfg,box.root)
        send.assert_not_called()


def test_actual_sender_exhausts_rate_limit_after_three_attempts(tmp_path,monkeypatch):
    import futures_trend_quant.live.paper_notify as notify
    from test_paper_notify import HOOK
    from unittest.mock import Mock
    cfg=tmp_path/'notify.json';cfg.write_text(json.dumps(dict(enabled=True,webhook=HOOK)))
    box=FeishuOutbox(cfg,tmp_path,start=False);key=box.enqueue('EVENT',message('验收',[]))
    send=Mock(return_value=dict(status='RATE_LIMITED',vendor_code=11232));monkeypatch.setattr(notify,'send_once',send)
    def finish(_):raise RuntimeError('END')
    monkeypatch.setattr(notify.time,'sleep',finish)
    for attempt in range(3):
        path=box.root/(key+'.json');row=json.loads(path.read_text(encoding='utf8'));row.pop('retry_at',None);notify.atomic_json(path,row)
        with pytest.raises(RuntimeError,match='END'):notify.sender(cfg,box.root)
    assert json.loads(path.read_text(encoding='utf8'))['status']=='RATE_LIMIT_EXHAUSTED'
    with pytest.raises(RuntimeError,match='END'):notify.sender(cfg,box.root)
    assert send.call_count==3


def test_sender_health_failure_pauses_without_restart_or_clearing_protection(tmp_path):
    from futures_trend_quant.live.paper_five import FiveRunner
    from unittest.mock import Mock
    r=FiveRunner.__new__(FiveRunner);r.readonly=False;r.c={'notification_policy_version':POLICY};r.started=NOW
    from test_paper_execution import engine
    r.engine,_=engine(tmp_path/'actual_engine');r.event=Mock()
    r.notifications=SimpleNamespace(health=lambda:dict(worker_alive=True,health_fresh=False))
    r.supervise_notification_sender(NOW+timedelta(seconds=61))
    assert r.engine.s['pause_new'] and r.notification_sender_problem=='FEISHU_SENDER_HEALTH_STALE'
    r.notifications=SimpleNamespace(health=lambda:dict(worker_alive=True,health_fresh=True))
    r.supervise_notification_sender(NOW+timedelta(seconds=70))
    assert r.engine.s['pause_new']  # Recovery is not authorization to clear protection.
