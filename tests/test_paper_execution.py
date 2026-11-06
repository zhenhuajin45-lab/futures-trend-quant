"""Actual paper gate/state-machine regressions, no GM network or orders."""
from copy import deepcopy
from pathlib import Path
import json
import pandas as pd
import pytest
from futures_trend_quant.live.paper_calendar import PaperCalendar
from futures_trend_quant.live.paper_broker import PaperEngine
from futures_trend_quant.live.paper_config import load_paper_config,digest,ROOT

ACCOUNT='00000000-0000-0000-0000-000000000001'
NOW=pd.Timestamp('2026-11-05T10:00:00+08:00')
SESSIONS=[dict(start='21:00',end='23:00'),dict(start='9:00',end='10:15'),dict(start='10:30',end='11:30'),dict(start='13:30',end='15:00')]


class FakeTransport:
    def __init__(self):self.jobs=[];self.is_busy=False;self.is_hung=False
    def request(self,op,args,rid=None):self.jobs.append(dict(id=rid,op=op,args=args,account_id=ACCOUNT));return rid
    def busy(self):return self.is_busy
    def hung(self):return self.is_hung


def calendar():return PaperCalendar(['2026-11-04','2026-11-05','2026-11-06','2026-11-09','2026-11-10','2026-11-11','2026-11-12','2026-11-13','2026-11-16'],SESSIONS)


def configuration(tmp_path):
    return dict(account_id=ACCOUNT,config_sha256='SYNTHETIC',state_dir=str(tmp_path/'state'),evidence_dir=str(tmp_path/'evidence'),
        quote_max_age_seconds=3,max_spread_ticks=4,open_wait_seconds=10,signal_max_age_seconds=30,max_positions=2,
        risk_pct=.0025,margin_usage_pct=.2,daily_loss_pct=.01,observation_drawdown_pct=.05)


def snapshot(orders=None,positions=None,active=None,fills=None,nav=1000000):
    return dict(cash=dict(account_id=ACCOUNT,nav=nav,available=nav-100,cum_commission=0),
        orders=orders or [],positions=positions or [],active=active or [],fills=fills or [])


def quote(now=NOW):return dict(created_at=str(now),bid=100.,ask=101.,tick=1.,bid_size=50,ask_size=50,last=101)


def signal(now=NOW,eid='E1'):
    return dict(symbol='DCE.m2701',direction='bull',evidence_id=eid,signal_at=str(now),anchor=99.,atr=2.,
        multiplier=10.,margin_ratio=.07,rules=dict(stop_buffer_atr=.6,stop_min_atr=1.5,stop_max_atr=6),path='D',m15_index=1234,
        anchor_bar_id='DCE.m2701|900s|'+str((now-pd.Timedelta(minutes=30)).isoformat()),anchor_confirmed_at=(now-pd.Timedelta(minutes=15)).isoformat(),
        anchor_episode_id='SYNTHETIC_EPISODE',trend_episode_id='SYNTHETIC_EPISODE',anchor_validated=True)


def engine(tmp_path):
    t=FakeTransport();e=PaperEngine(configuration(tmp_path),calendar(),t)
    e.apply_snapshot(snapshot(),NOW);e.s['connectivity_passed']=True;e.quote('DCE.m2701',quote(),NOW)
    return e,t


def ack(e,status=1,filled=0,oid='O1',now=NOW):
    i=next(x for x in e.s['intents'].values() if x['effect']==1)
    row=dict(account_id=ACCOUNT,cl_ord_id=oid,symbol=i['symbol'],side=i['side'],position_effect=1,
        volume=1,price=i['price'],status=status,filled_volume=filled,filled_vwap=101 if filled else 0,
        created_at=str(NOW),updated_at=str(now))
    e.accept(dict(op='submit',id=i['id']),dict(ok=True,data=[row]),now)
    return row


def position():return dict(account_id=ACCOUNT,symbol='DCE.m2701',side=1,volume=1,available=1,volume_today=1,vwap=101.)


def test_persist_intent_before_actual_transport_send(tmp_path):
    e,t=engine(tmp_path)
    def send(op,args,rid):
        persisted=json.loads(e.path.read_text(encoding='utf8'))
        assert rid in persisted['intents'] and 'E1' in persisted['used_signals']
        t.jobs.append(dict(op=op,args=args,id=rid))
    t.request=send
    ok,_=e.open(signal(),NOW)
    assert ok and t.jobs[0]['args']['volume']==1 and t.jobs[0]['args']['price']==101


def test_duplicate_signal_and_second_product_intent_never_add(tmp_path):
    e,t=engine(tmp_path);assert e.open(signal(),NOW)[0]
    assert e.open(signal(),NOW)[1]=='DUPLICATE_SIGNAL'
    assert e.open(signal(eid='E2'),NOW)[1]=='PRODUCT_POSITION_OR_OPEN_INTENT'
    assert len(t.jobs)==1


def test_wrong_or_missing_account_rejected_before_any_order(tmp_path):
    e,t=engine(tmp_path);s=snapshot();s['cash'].pop('account_id')
    e.accept(dict(op='snapshot'),dict(ok=True,data=s),NOW)
    assert not e.ready and e.open(signal(),NOW)[1]=='PENDING_RECON' and not t.jobs


def test_partial_status_is_managed_and_cancel_does_not_erase_fill(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=2,filled=1)
    e.apply_snapshot(snapshot(orders=[row],positions=[position()],active=[row]),NOW)
    row.update(status=5)
    e.apply_snapshot(snapshot(orders=[row],positions=[position()]),NOW)
    i=next(iter(e.s['intents'].values()))
    assert i['filled']==1 and e.positions()[0]['volume']==1 and e.s['meta']['DCE.m2701|bull']['stop']==97


def test_repeated_and_out_of_order_reports_do_not_undo_terminal_or_fills(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    e.on_order(row,NOW);e.on_order(dict(row,status=1,filled_volume=0,filled_vwap=0),NOW)
    e.on_order(dict(row,status=2),NOW)
    i=next(iter(e.s['intents'].values()));assert i['status']==3 and i['filled']==1
    log=(e.evidence/'order_events.jsonl').read_text(encoding='utf8')
    assert log.count('CUMULATIVE_FILL_INCREMENT')==1


@pytest.mark.parametrize('status',[5,8,12])
def test_zero_fill_terminal_releases_exposure_but_not_signal_identity(tmp_path,status):
    e,t=engine(tmp_path);e.open(signal(),NOW);ack(e,status=status)
    assert not e.active() and not e.s['meta'] and 'E1' in e.s['used_signals']


def test_unknown_submit_restart_reconciles_exact_order_without_resending(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW)
    e2=PaperEngine(e.c,calendar(),FakeTransport())
    i=next(iter(e2.s['intents'].values()))
    row=dict(account_id=ACCOUNT,cl_ord_id='RECOVERED',symbol=i['symbol'],side=1,position_effect=1,price=101.,
        volume=1,created_at=str(NOW),updated_at=str(NOW),status=3,filled_volume=1,filled_vwap=101.)
    e2.apply_snapshot(snapshot(orders=[row],positions=[position()]),NOW)
    assert e2.ready and i['order_id']=='RECOVERED' and not e2.transport.jobs


def test_unknown_submit_no_broker_match_stays_pending_and_never_retries(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW)
    e2=PaperEngine(e.c,calendar(),FakeTransport())
    with pytest.raises(ValueError,match='submit outcome unknown'):e2.apply_snapshot(snapshot(),NOW)
    assert not e2.ready and not e2.transport.jobs


def test_restart_restores_stops_deadline_used_signal_and_position(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    e.apply_snapshot(snapshot(orders=[row],positions=[position()]),NOW)
    e2=PaperEngine(e.c,calendar(),FakeTransport());e2.apply_snapshot(snapshot(orders=[row],positions=[position()]),NOW)
    assert e2.s['meta']==e.s['meta'] and e2.s['used_signals']==['E1']
    assert e2.s['meta']['DCE.m2701|bull']['deadline'].startswith('2026-11-06T14:55')


def test_unowned_position_blocks_entries_no_blind_close(tmp_path):
    e,t=engine(tmp_path)
    e.accept(dict(op='snapshot'),dict(ok=True,data=snapshot(positions=[position()])),NOW)
    e.tick(NOW)
    assert not e.ready and not t.jobs


def test_deadline_fires_without_quote_and_does_not_claim_flat(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    later=pd.Timestamp('2026-11-06T14:55:00+08:00')
    e.apply_snapshot(snapshot(orders=[row],positions=[position()]),later);e.quotes.clear();t.jobs.clear()
    e.tick(later)
    assert e.s['due_events'] and not any(x['completed'] for x in e.s['due_events'].values())
    assert not t.jobs and e.positions()[0]['volume']==1


def test_close_intent_not_completion_only_broker_flat_and_no_active_is_completion(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    e.apply_snapshot(snapshot(orders=[row],positions=[position()]),NOW)
    e.s['meta']['DCE.m2701|bull']['force_exit']='TEST_EXIT';e.tick(NOW)
    close=t.jobs[-1];assert close['args']['position_effect']==2
    assert not next(iter(e.s['due_events'].values()))['completed']
    i=e.s['intents'][close['id']]
    r=dict(row,cl_ord_id='CLOSE1',side=2,position_effect=2,price=100.,status=3,filled_volume=1,filled_vwap=100.)
    e.accept(close,dict(ok=True,data=[r]),NOW)
    e.apply_snapshot(snapshot(orders=[row,r]),NOW);e.tick(NOW)
    assert next(iter(e.s['due_events'].values()))['completed']


def test_late_open_fill_after_cancel_creates_managed_exposure(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=5,filled=0)
    i=next(iter(e.s['intents'].values()));i['force_exit']='DAILY_CLOSE_WINDOW_NO_ENTRY';e.save()
    later=NOW+pd.Timedelta(seconds=2);row.update(status=3,filled_volume=1,filled_vwap=101.,updated_at=str(later))
    e.apply_snapshot(snapshot(orders=[row],positions=[position()]),later)
    e.quote('DCE.m2701',quote(later),later);e.tick(later)
    assert any(j['args'].get('position_effect')==2 for j in t.jobs)


def test_stale_quote_and_expired_or_future_signal_do_not_place(tmp_path):
    e,t=engine(tmp_path)
    assert e.open(signal(NOW-pd.Timedelta(seconds=31)),NOW)[1]=='SIGNAL_EXPIRED_OR_FUTURE'
    assert e.open(signal(NOW+pd.Timedelta(seconds=1)),NOW)[1]=='SIGNAL_EXPIRED_OR_FUTURE'
    e.quote('DCE.m2701',quote(NOW-pd.Timedelta(seconds=4)),NOW)
    assert e.open(signal(),NOW)[1]=='QUOTE_STALE_OR_FUTURE' and not t.jobs


def test_daily_loss_and_drawdown_stop_only_new_orders(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    e.apply_snapshot(snapshot(orders=[row],positions=[position()],nav=940000),NOW)
    assert e.s['halt_reason']=='OBSERVATION_DRAWDOWN_LIMIT'
    e.s['meta']['DCE.m2701|bull']['force_exit']='STOP';e.tick(NOW)
    assert any(x['args'].get('position_effect')==2 for x in t.jobs)


def test_friday_and_holiday_boundaries_use_trading_days_not_natural_days():
    c=calendar()
    assert c.day('2026-11-06T21:10:00+08:00')=='2026-11-09'
    assert c.entry_block('2026-11-06T21:10:00+08:00')=='PRE_LONG_CLOSURE_NO_REOPEN'
    assert c.deadline('2026-11-06').startswith('2026-11-09T14:55')
    assert c.due('2026-11-06','2026-11-06T14:55:00+08:00')[1]=='PRE_LONG_CLOSURE_EXIT'
    assert c.day('2026-11-06T15:10:00+08:00')=='2026-11-06'
    holiday=PaperCalendar(['2026-11-04','2026-11-05','2026-11-10','2026-11-11'],SESSIONS)
    assert holiday.entry_block('2026-11-05T14:55:00+08:00')=='PRE_LONG_CLOSURE_NO_REOPEN'
    assert not any(x.date().isoformat()=='2026-11-05' and x.hour>=21 for x in holiday.clock)


@pytest.mark.parametrize('patch',[
    {'live_orders_enabled':True},{'paper_orders_enabled':False},{'endpoint':'localhost:7001'},
    {'endpoint':''},{'max_lots':2},{'risk_pct':.01},{'universe':['DCE.M','DCE.JD']},
])
def test_paper_config_rejects_live_default_and_scope_drift(tmp_path,patch):
    (tmp_path/'config').mkdir()
    base=ROOT/'config/strategy.json';(tmp_path/'config/strategy.json').write_bytes(base.read_bytes())
    proof=tmp_path/'config/paper_account.local.json'
    proof.write_text(json.dumps(dict(account_id=ACCOUNT,endpoint='api.myquant.cn:9000',source='GM_OFFICIAL_SIM_ACCOUNT_CASH_ID_MATCH',user_designated_futures_sim=True)))
    c=json.loads((ROOT/'config/paper_observation.example.json').read_text())
    c['account_binding_sha256']=digest(proof);c.update(patch)
    path=tmp_path/'config/paper_observation.local.json';path.write_text(json.dumps(c))
    with pytest.raises(ValueError):load_paper_config(path,root=tmp_path)


def test_changed_account_binding_rejected(tmp_path):
    (tmp_path/'config').mkdir();proof=tmp_path/'config/paper_account.local.json';proof.write_text('{}')
    c=json.loads((ROOT/'config/paper_observation.example.json').read_text());c['account_binding_sha256']='wrong'
    path=tmp_path/'p.json';path.write_text(json.dumps(c))
    with pytest.raises(ValueError,match='binding changed'):load_paper_config(path,root=tmp_path)


def test_unknown_order_does_not_disable_confirmed_position_reduction(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    unknown=dict(row,cl_ord_id='FOREIGN_INTENT',price=1.,filled_volume=0,status=1)
    e.accept(dict(op='snapshot'),dict(ok=True,data=snapshot(orders=[row,unknown],positions=[position()])),NOW)
    assert not e.ready
    e.s['meta']['DCE.m2701|bull']['force_exit']='CONFIRMED_RISK_REDUCTION'
    e.tick(NOW)
    assert t.jobs[-1]['args']['position_effect']==2


def test_position_fill_mismatch_freezes_open_until_consistent_snapshot(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    e.accept(dict(op='snapshot'),dict(ok=True,data=snapshot(orders=[row])),NOW)
    assert not e.ready
    e.apply_snapshot(snapshot(orders=[row],positions=[position()]),NOW)
    assert e.ready


def test_duplicate_execution_ids_written_only_once(tmp_path):
    import csv
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    fill=dict(account_id=ACCOUNT,exec_id='X1',cl_ord_id='O1',symbol='DCE.m2701',exec_type=15,
        side=1,position_effect=1,volume=1,price=101,commission=1,created_at=str(NOW))
    s=snapshot(orders=[row],positions=[position()],fills=[fill])
    e.apply_snapshot(s,NOW);e.apply_snapshot(s,NOW)
    with (e.evidence/'fills.csv').open(encoding='utf8') as f:assert len(list(csv.DictReader(f)))==1


def test_write_ahead_failure_prevents_sending_an_order(tmp_path,monkeypatch):
    from futures_trend_quant.live import paper_broker
    e,t=engine(tmp_path)
    def fail(*a,**k):raise OSError('simulated disk failure')
    monkeypatch.setattr(paper_broker,'atomic',fail)
    with pytest.raises(OSError):e.open(signal(),NOW)
    assert not t.jobs


def test_old_filled_order_cannot_overwrite_later_episode_stop(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    old=next(iter(e.s['intents'].values()))
    new=dict(old,id='NEW_EPISODE',order_id='NEW_ORDER',stop=90,filled=0)
    e.s['intents'][new['id']]=new
    e.on_order(dict(row,cl_ord_id='NEW_ORDER'),NOW)
    assert e.s['meta']['DCE.m2701|bull']['stop']==90
    e.on_order(row,NOW)
    assert e.s['meta']['DCE.m2701|bull']['stop']==90


def test_stale_broker_active_row_blocks_flat_completion(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=5,filled=0)
    stale=dict(row,status=1)
    e.accept(dict(op='snapshot'),dict(ok=True,data=snapshot(orders=[row],active=[stale])),NOW)
    assert not e.ready and e.block=='PENDING_RECON'


def test_recovery_cannot_bind_a_much_later_external_order(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW)
    i=next(iter(e.s['intents'].values()))
    row=dict(account_id=ACCOUNT,cl_ord_id='LATER_EXTERNAL',symbol=i['symbol'],side=i['side'],
        position_effect=1,price=i['price'],volume=1,filled_volume=0,status=1,
        created_at=str(NOW+pd.Timedelta(hours=1)))
    with pytest.raises(ValueError,match='unknown/ambiguous'):e.on_order(row,NOW+pd.Timedelta(hours=1))
    assert not i['order_id']


def test_broker_rejection_and_first_fill_clock_are_preserved(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=8,filled=0)
    e.on_order(dict(row,ord_rej_reason=42,ord_rej_reason_detail='synthetic reject'),NOW)
    # A repeated terminal report can add text, but cannot create a second order.
    assert len(t.jobs)==1
    assert next(iter(e.s['intents'].values()))['broker_reject_detail']=='synthetic reject'
    assert 'ORDER_STATUS' in (e.evidence/'order_events.jsonl').read_text()
    e2,t2=engine(tmp_path/'filled');e2.open(signal(),NOW);ack(e2,status=3,filled=1)
    i=next(iter(e2.s['intents'].values()))
    assert i['first_fill_received_at']==str(NOW)
    assert i['fill_vs_quoted_opponent_ticks']==0


def test_unknown_position_price_keeps_known_execution_price_and_blocks_new(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    e.accept(dict(op='snapshot'),dict(ok=True,data=snapshot(orders=[row],positions=[dict(position(),vwap=0)])),NOW)
    assert not e.ready and e.s['meta']['DCE.m2701|bull']['entry_price']==101


def test_unknown_extra_lot_is_not_closed_after_owned_lot_was_reduced(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    e.apply_snapshot(snapshot(orders=[row],positions=[position()]),NOW)
    first=next(iter(e.s['intents'].values()))
    e.s['intents']['ALREADY_CLOSED']=dict(first,id='ALREADY_CLOSED',effect=2,status=3,filled=1,order_id='C1')
    e.ready=False;e.s['meta']['DCE.m2701|bull']['force_exit']='STOP'
    e.tick(NOW)
    assert len(t.jobs)==1
    assert e.s['meta']['DCE.m2701|bull']['unowned_residual_logged']


@pytest.mark.parametrize('flag',['covered_flag','hedge_flag'])
def test_unsupported_position_property_cannot_use_generic_close(tmp_path,flag):
    e,t=engine(tmp_path);e.open(signal(),NOW);row=ack(e,status=3,filled=1)
    e.accept(dict(op='snapshot'),dict(ok=True,data=snapshot(orders=[row],positions=[dict(position(),**{flag:1})])),NOW)
    e.s['meta']['DCE.m2701|bull']['force_exit']='STOP';e.tick(NOW)
    assert not e.ready and len(t.jobs)==1


def test_actual_entry_refuses_timed_shutdown_before_loading_accounts():
    import subprocess,sys
    result=subprocess.run([sys.executable,str(ROOT/'deploy/gm_paper_main.py'),'--config','DOES_NOT_EXIST','--max-seconds','1'],
        capture_output=True,text=True)
    assert result.returncode==2 and 'only allowed for readonly' in result.stderr


def test_base_configuration_fingerprint_rejects_parameter_edits(tmp_path):
    import shutil
    from futures_trend_quant.live.paper_config import frozen_core_digest
    (tmp_path/'config').mkdir()
    shutil.copytree(ROOT/'futures_trend_quant/core',tmp_path/'futures_trend_quant/core')
    (tmp_path/'tools/short_swing_v9').mkdir(parents=True)
    shutil.copyfile(ROOT/'tools/short_swing_v9/policy.py',tmp_path/'tools/short_swing_v9/policy.py')
    base=tmp_path/'config/strategy.json';base.write_bytes((ROOT/'config/strategy.json').read_bytes())
    proof=tmp_path/'config/paper_account.local.json'
    proof.write_text(json.dumps(dict(account_id=ACCOUNT,endpoint='api.myquant.cn:9000',source='GM_OFFICIAL_SIM_ACCOUNT_CASH_ID_MATCH',user_designated_futures_sim=True)))
    c=json.loads((ROOT/'config/paper_observation.example.json').read_text());c['account_binding_sha256']=digest(proof)
    c['core_policy_sha256_lf']=frozen_core_digest(tmp_path)
    path=tmp_path/'config/paper_observation.local.json';path.write_text(json.dumps(c))
    assert load_paper_config(path,root=tmp_path)['account_id']==ACCOUNT
    changed=json.loads(base.read_text());changed['rules']['stop_min_atr']=2.;base.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='base S0'):load_paper_config(path,root=tmp_path)
