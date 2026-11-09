"""Production-shaped SDK tick and asynchronous final-submit regressions.

Real GM TickLikeDict2 is instantiated offline; all account/order API calls are
synthetic fakes in isolated temporary directories. No broker connection.
"""
import json,queue,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pandas as pd
import pytest
from gm.csdk.c_sdk import TickLikeDict2
from futures_trend_quant.live import gm_paper_host as host
from futures_trend_quant.live.paper_five import FiveRunner
from futures_trend_quant.live.paper_service_health import require_service,ServiceProofError
from futures_trend_quant.live.paper_quote_feed import TICK_SOURCE
from futures_trend_quant.live.paper_cards import intent_plan,candidate_card,intent_reference
from tools.paper_trend_notify import trend_risk
from test_paper_runtime_health_v6 import runner as quote_runner,tick,setup_health,write
from test_paper_service_health import runner as service_runner
from test_paper_execution import signal,NOW
from test_paper_bridge import invoke,req


def test_real_gm_tick_like_dict_without_get_reaches_original_engine_and_native_cache(tmp_path):
    r,t=quote_runner(tmp_path);payload=TickLikeDict2(tick())
    assert not callable(getattr(payload,'get',None))
    with pytest.raises(TypeError):payload.get('quotes')
    host.on_tick(SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False),payload)
    r.market_events(NOW)
    assert r.engine.quotes['DCE.m2701']['source']==TICK_SOURCE
    assert json.loads((r.evidence/'latest_quotes/DCE.m2701.json').read_text())['quote']['bid']==100
    assert not t.jobs


def test_tick_error_diagnostic_is_counted_but_not_written_per_tick(tmp_path,monkeypatch):
    r,t=quote_runner(tmp_path);r.event=Mock();c=SimpleNamespace(ftq_paper_runner=r,ftq_paper_stopped=False)
    monkeypatch.setattr(host.time,'monotonic',lambda:10.)
    for _ in range(100):host.on_tick(c,TickLikeDict2(dict(symbol='DCE.m2701')))
    assert r.event.call_count==1 and r.tick_rejection_summary['total']==100
    assert r.quote_feed_counts['GM_HOST_TICK_REJECTED']==100 and r.host_ticks.empty()
    assert r.event.call_args.kwargs['payload_type']=='TickLikeDict2'


def armed(tmp_path):
    r,t=service_runner(tmp_path,at=NOW);setup_health(r.c);r.run_id='CURRENT'
    r.engine.quote('DCE.m2701',dict(created_at=str(NOW),bid=100.,ask=101.,tick=1.,bid_size=2,ask_size=2,last=101),NOW)
    r.transport.writer=SimpleNamespace(health=lambda:dict(state='PENDING',op='submit',rpc_id=t.jobs[-1]['id']))
    ok,rid=r.engine.open(signal(),NOW);assert ok
    r.service_health.update(NOW+pd.Timedelta(seconds=.2))
    return r,t,rid


def test_same_pending_request_can_validate_but_global_new_entry_gate_stays_closed(tmp_path):
    r,t,rid=armed(tmp_path);state=r.service_health.state
    assert not state['entry_gate_ready'] and state['pending_entry_request']['request_id']==rid
    with pytest.raises(ServiceProofError):require_service(r.c,NOW+pd.Timedelta(seconds=.3))
    args=dict(t.jobs[-1]['args'],request_id=rid)
    assert require_service(r.c,NOW+pd.Timedelta(seconds=.3),args)['request_id']==rid
    assert len(t.jobs)==1 and not r.engine.s['pause_new']


@pytest.mark.parametrize('mutation',['wrong_request','wrong_symbol','wrong_price','unknown_submit','unknown_cancel','other_intent','old_account','old_proof','pause','active_service'])
def test_bound_request_never_bypasses_unknown_or_stale_or_foreign_or_risk(tmp_path,mutation):
    r,t,rid=armed(tmp_path);args=dict(t.jobs[-1]['args'],request_id=rid);at=NOW+pd.Timedelta(seconds=.3)
    row=r.service_health.state.copy();i=r.engine.s['intents'][rid]
    if mutation=='wrong_request':args['request_id']='FOREIGN'
    elif mutation=='wrong_symbol':args['symbol']='DCE.y2701'
    elif mutation=='wrong_price':args['price']+=1
    elif mutation=='unknown_submit':i['native_submit_invoked']='UNKNOWN'
    elif mutation=='unknown_cancel':i['cancel_outcome_unknown']=True
    elif mutation=='other_intent':r.engine.s['intents']['OTHER']=dict(i,id='OTHER')
    elif mutation=='old_account':row['account_verified_at']=str(NOW-pd.Timedelta(seconds=4))
    elif mutation=='old_proof':row['at']=str(NOW-pd.Timedelta(seconds=4))
    elif mutation=='pause':r.engine.s['pause_new']=True
    elif mutation=='active_service':row['active']={'id':'FAULT'}
    r.engine.save();write(r.evidence/'service_health.local.json',row)
    with pytest.raises(ServiceProofError) as err:require_service(r.c,at,args)
    assert err.value.details['failed_condition'] and err.value.details['validated_at']
    assert len(t.jobs)==1


def test_real_bridge_after_coordinator_next_cycle_reaches_fake_native_once(tmp_path,monkeypatch):
    """WAL -> next real coordinator cycle -> final native guard -> ACK apply."""
    r,t=service_runner(tmp_path/'engine',at=NOW);requests=[];created={}
    extra=dict(clock_policy_version='PAPER_CLOCK_HEALTH_V1_20261109',execution_policy_version='PAPER_EXEC_V5',
        state_dir=r.engine.c['state_dir'],evidence_dir=r.engine.c['evidence_dir'])
    def before(root,config):
        from futures_trend_quant.live.paper_config import digest
        r.c['config_sha256']=digest(config);r.engine.s['config_sha256']=digest(config);r.engine.save()
        setup_health(r.c);r.run_id='CURRENT'
        assert r.engine.offer(signal(),NOW)[1]=='CANDIDATE_WAITING'
        r.engine.drain_candidates(NOW);job=t.jobs[-1];created.update(job)
        r.transport.writer=SimpleNamespace(health=lambda:dict(state='PENDING',op='submit',rpc_id=job['id']))
        q=dict(r.engine.quotes['DCE.m2701'],source='GM_BULK_CURRENT_POLL')
        write(r.evidence/'latest_quotes/DCE.m2701.json',dict(symbol='DCE.m2701',config_sha256=digest(config),received_at=str(NOW),quote=q))
        requests.append(req('submit',job['id'],arguments=job['args']))
    def next_cycle():
        alive=SimpleNamespace(is_alive=lambda:True)
        for p in r.products.values():p.update(market=SimpleNamespace(thread=alive),core=SimpleNamespace(thread=alive,inbox=queue.Queue()),metadata=None,last_positions={})
        r.transport.drain=lambda:[];r.transport.busy=lambda:True;r.transport.hung=lambda:False
        r.engine.transport=r.transport;r.transport.request=lambda *a,**kw:None
        r.quote_worker=alive;r.last_loop=time.monotonic();r.max_gap=0;r.readonly=False
        r.market_events=Mock();r.core_events=Mock();r.restore_strategy_fills=Mock();r.pump_notifications=Mock();r.supervise_clock=Mock()
        r.last_problem=None;r.last_poll=time.monotonic();r.last_report=time.monotonic()
        monkeypatch.setattr('futures_trend_quant.live.paper_five.pd',SimpleNamespace(Timestamp=SimpleNamespace(now=lambda **kw:NOW+pd.Timedelta(seconds=.2))))
        FiveRunner.cycle(r)
        assert not r.service_health.state['entry_gate_ready'] and r.service_health.state['pending_entry_request']
    class Clock:
        fromisoformat=staticmethod(__import__('datetime').datetime.fromisoformat)
        @staticmethod
        def now(tz):return (NOW+pd.Timedelta(seconds=.3)).to_pydatetime()
    replies,calls=invoke(tmp_path,monkeypatch,requests,profile_extra=extra,before_serve=before,cash_hook=next_cycle,clock=Clock)
    assert replies[0]['ok'] and replies[0]['native_submit_invoked'] is True
    assert len([x for x in calls if x[0]=='submit'])==1
    replies[0]['data'][0].update(status=1,filled_volume=0,filled_vwap=0,side=1,position_effect=1,
        created_at=str(NOW),updated_at=str(NOW+pd.Timedelta(seconds=.4)))
    r.engine.accept(created,replies[0],NOW+pd.Timedelta(seconds=.4))
    i=r.engine.s['intents'][created['id']]
    assert i['order_id']=='ORDER1' and i['filled']==0
    # An ACK is not a fill and this evidence cannot create another request.
    assert r.engine.open(signal(),NOW+pd.Timedelta(seconds=.5))[1]=='DUPLICATE_SIGNAL'


def test_local_reject_updates_candidate_terminal_and_linked_card(tmp_path,monkeypatch):
    r,t=service_runner(tmp_path,at=NOW);e=r.engine
    e.offer(signal(),NOW);e.drain_candidates(NOW);job=t.jobs[-1];rid=job['id']
    e.accept(job,dict(ok=False,submit_may_have_reached_broker=False,native_submit_invoked=False,
        reason='SERVICE_RECOVERY_PENDING:ACCOUNT_AGE',service_diagnostic={'failed_condition':'ACCOUNT_AGE'}),NOW)
    assert e.s['candidate_status']['E1']['status']=='LOCAL_REJECTED_BEFORE_NATIVE'
    assert e.s['intents'][rid]['service_diagnostic']['failed_condition']=='ACCOUNT_AGE'
    i=e.s['intents'][rid];p=intent_plan(i,e.c);card=candidate_card(p,(True,rid),NOW,stage='ORDER_INTENT')
    assert intent_reference(rid) in json.dumps(card,ensure_ascii=False)
    assert '本地入场意图' in card['card']['header']['title']['content'] and '待执行回执' in card['card']['header']['title']['content']
    r.notifications=SimpleNamespace(enqueue=Mock());r.c['notification_policy_version']='ACTIONABLE_CARDS_V4_20261106';r.order_offset=0;r.cursor_path=tmp_path/'cursor.json';r.notice=Mock()
    FiveRunner.pump_notifications(r)
    reject=next(c for c in r.notice.call_args_list if '入场终止' in c.args[1])
    assert intent_reference(rid) in str(reject) and '未调用柜台下单' in str(reject)
    assert not any('执行或对账异常' in str(c) for c in r.notice.call_args_list)
    text=trend_risk(dict(direction='bull',entry={'accepted':True,'path':'D'},evidence={'entry_policy_version':'PAPER_ENTRY_V5','entry_permission':{'new_entry_allowed':True}},execution_terminal=e.s['candidate_status']['E1']))
    assert '未下单' in text and '已生成委托意图' not in text


def test_native_snapshot_trace_names_each_stage_without_values(tmp_path,monkeypatch):
    results,calls=invoke(tmp_path,monkeypatch,[req('snapshot')])
    rows=[json.loads(x) for x in (tmp_path/'evidence/paper_native/native_stages.local.jsonl').read_text().splitlines()]
    assert [x['stage'] for x in rows if x['phase']=='START']==['cash','positions','orders','active_orders','fills']
    assert all(x['rpc_id']=='R1' for x in rows) and all('account' not in x for x in rows)
    assert results[0]['ok'] and not any(x[0]=='submit' for x in calls)


@pytest.mark.parametrize('lane_state,allowed',[('IDLE',True),('SLOW_PENDING',False),('TRANSPORT_CLOSED',False),('PROCESS_EXITED',False)])
def test_queue_to_sent_race_is_bound_to_one_live_writer_request(tmp_path,lane_state,allowed):
    r,t,rid=armed(tmp_path)
    r.transport.writer=SimpleNamespace(health=lambda:dict(state=lane_state,op=None,rpc_id=None),
        describe_busy=lambda:dict(waiting=[dict(op='submit',rpc_id=rid)]))
    r.service_health.publish(NOW+pd.Timedelta(seconds=.3),force=True)
    assert bool(r.service_health.state['pending_entry_request']) is allowed
    assert r.service_health.state['entry_gate_ready'] is False


def test_writer_started_between_health_and_queue_snapshot_does_not_revoke_own_request(tmp_path):
    r,t,rid=armed(tmp_path)
    r.transport.writer=SimpleNamespace(health=lambda:dict(state='IDLE',op=None,rpc_id=None),
        describe_busy=lambda:dict(current=dict(op='submit',rpc_id=rid),waiting=[]))
    r.service_health.publish(NOW+pd.Timedelta(seconds=.3),force=True)
    assert r.service_health.state['pending_entry_request']['request_id']==rid
    assert not r.service_health.state['entry_gate_ready']


def test_unknown_submission_is_not_misreported_as_local_rejection_and_never_reissued(tmp_path):
    r,t=service_runner(tmp_path,at=NOW);e=r.engine;e.offer(signal(),NOW);e.drain_candidates(NOW);job=t.jobs[-1]
    e.accept(job,dict(ok=False,native_submit_invoked=True,submit_may_have_reached_broker=True,
        reason='SIM_CONNECTION_ERROR'),NOW)
    assert e.s['candidate_status']['E1']['status']=='SUBMIT_OUTCOME_UNKNOWN'
    assert e.s['intents'][job['id']]['status']==10
    e.drain_candidates(NOW+pd.Timedelta(seconds=1));assert len(t.jobs)==1
    text=trend_risk(dict(direction='bull',entry={'accepted':True,'path':'D'},evidence={'entry_policy_version':'PAPER_ENTRY_V5','entry_permission':{'new_entry_allowed':True}},execution_terminal=e.s['candidate_status']['E1']))
    assert '提交结果未知' in text and '未下单' not in text


def test_old_waiting_card_is_superseded_without_rewriting_unknown_delivery(tmp_path):
    from futures_trend_quant.live.paper_notify import FeishuOutbox,superseded_before_send
    bot=FeishuOutbox(tmp_path/'config.json',tmp_path,start=False)
    key=bot.enqueue('INTENT',{},intent_id='REQUEST');path=bot.root/(key+'.json')
    row=json.loads(path.read_text());row['status']='PENDING'
    bot.supersede_intent('REQUEST','LOCAL_REJECTED_BEFORE_NATIVE')
    assert superseded_before_send(row,bot.root,NOW.to_pydatetime()) and row['status']=='SUPERSEDED_NOT_SENT'
    for status in ['SENDING','SENT','UNKNOWN_DELIVERY']:
        untouched=dict(row,status=status)
        assert not superseded_before_send(untouched,bot.root,NOW.to_pydatetime()) and untouched['status']==status
