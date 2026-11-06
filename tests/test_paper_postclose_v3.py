"""Production-shaped execution regressions, no network or generated signals."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json
import pandas as pd
import pytest
from test_paper_execution import engine,signal,quote,snapshot,position,ACCOUNT,NOW,FakeTransport
from futures_trend_quant.live.paper_broker import PaperEngine
from futures_trend_quant.live.paper_risk_controls import VERSION,cooldown_reason,observe_shadow
from futures_trend_quant.live.paper_quote_cache import latest_quote
from futures_trend_quant.core.models import Bar


def roundtrip(e,at,key,exit_price=100,commission=0,tag='STRATEGY'):
    e.apply_snapshot(snapshot(),at);e.quote('DCE.m2701',quote(at),at)
    ok,rid=e.open(signal(at,key),at,tag=tag);assert ok
    i=e.s['intents'][rid]
    opening=dict(account_id=ACCOUNT,cl_ord_id='OPEN'+key,symbol=i['symbol'],side=1,position_effect=1,
                 volume=1,price=101.,status=3,filled_volume=1,filled_vwap=101.,created_at=str(at),updated_at=str(at))
    e.accept(dict(op='submit',id=rid),dict(ok=True,data=[opening]),at)
    e.apply_snapshot(snapshot(orders=[opening],positions=[position()]),at)
    e.s['meta']['DCE.m2701|bull']['force_exit']='SYNTHETIC_CLOSE'
    e.tick(at);close=next(j for j in reversed(e.transport.jobs) if j['op']=='submit' and j['args']['position_effect']!=1)
    ci=e.s['intents'][close['id']]
    closing=dict(opening,cl_ord_id='CLOSE'+key,side=2,position_effect=2,price=ci['price'],filled_vwap=exit_price)
    e.accept(dict(op='submit',id=ci['id']),dict(ok=True,data=[closing]),at)
    fills=[dict(account_id=ACCOUNT,exec_id=oid,cl_ord_id=oid,symbol=i['symbol'],side=side,position_effect=effect,
                volume=1,price=price,commission=commission,created_at=str(at))
           for oid,side,effect,price in [('OPEN'+key,1,1,101.),('CLOSE'+key,2,2,exit_price)]]
    e.apply_snapshot(snapshot(orders=[opening,closing],fills=fills),at)
    return opening,closing,fills


def test_three_losses_persist_cooldown_and_never_close_held_positions(tmp_path):
    e,t=engine(tmp_path);e.c['execution_policy_version']=VERSION
    for n in range(3):roundtrip(e,NOW+pd.Timedelta(minutes=n),str(n))
    risk=e.s['loss_cooldown'];assert risk['consecutive_losses']==3
    now=NOW+pd.Timedelta(minutes=3)
    e.quote('DCE.m2701',quote(now),now);e.apply_snapshot(snapshot(),now)
    assert e.open(signal(now,'AFTER3'),now)[1]=='CONSECUTIVE_LOSS_COOLDOWN'
    e2=PaperEngine(e.c,e.calendar,FakeTransport())
    assert e2.s['loss_cooldown']==risk and len(e2.s['strategy_roundtrips'])==3
    assert cooldown_reason(e2.s,NOW+pd.Timedelta(minutes=62),e.calendar) is None


def test_duplicate_reconciliation_does_not_count_loss_twice(tmp_path):
    e,t=engine(tmp_path);e.c['execution_policy_version']=VERSION
    opening,closing,fills=roundtrip(e,NOW,'X')
    e.apply_snapshot(snapshot(orders=[opening,closing],fills=fills),NOW)
    assert e.s['loss_cooldown']['consecutive_losses']==1
    assert len(e.s['strategy_roundtrips'])==1


def test_fees_determine_platform_loss_and_engineering_excluded(tmp_path):
    e,t=engine(tmp_path);e.c['execution_policy_version']=VERSION
    roundtrip(e,NOW,'TEST',tag='CONNECTIVITY_TEST')
    assert not e.s.get('strategy_roundtrips')
    roundtrip(e,NOW+pd.Timedelta(minutes=1),'FEE',exit_price=102,commission=6)
    assert e.s['loss_cooldown']['consecutive_losses']==1
    assert next(iter(e.s['strategy_roundtrips'].values()))['platform_net']==-2


def test_zero_keeps_streak_win_resets_but_cooldown_latch_survives(tmp_path):
    e,t=engine(tmp_path);e.c['execution_policy_version']=VERSION
    roundtrip(e,NOW,'LOSS');roundtrip(e,NOW+pd.Timedelta(minutes=1),'ZERO',exit_price=101)
    assert e.s['loss_cooldown']['consecutive_losses']==1
    roundtrip(e,NOW+pd.Timedelta(minutes=2),'WIN',exit_price=102)
    assert e.s['loss_cooldown']['consecutive_losses']==0


def test_five_losses_block_same_trading_day_after_hour_expiry(tmp_path):
    e,t=engine(tmp_path);e.c['execution_policy_version']=VERSION
    for n,day in enumerate(['2026-11-05','2026-11-06','2026-11-09','2026-11-10','2026-11-11']):
        roundtrip(e,pd.Timestamp(day+'T10:00:00+08:00'),str(n))
    assert cooldown_reason(e.s,pd.Timestamp('2026-11-11T13:30:00+08:00'),e.calendar)=='CONSECUTIVE_LOSS_NEXT_TRADING_DAY'
    assert cooldown_reason(e.s,pd.Timestamp('2026-11-12T09:00:00+08:00'),e.calendar) is None


@pytest.mark.parametrize('halt',['DAILY_LOSS_LIMIT','OBSERVATION_DRAWDOWN_LIMIT'])
def test_risk_halt_cancels_live_pending_open_without_waiting_ten_seconds(tmp_path,halt):
    e,t=engine(tmp_path);e.open(signal(),NOW)
    i=next(iter(e.s['intents'].values()));i.update(order_id='O',status=1)
    e.s['halt_reason']=halt;e.tick(NOW+pd.Timedelta(seconds=.1))
    assert t.jobs[-1]['op']=='cancel' and i['force_exit']==halt
    assert not any(j['args'].get('position_effect')==2 for j in t.jobs)


def test_unknown_submission_halt_never_resends_or_claims_cancelled(tmp_path):
    e,t=engine(tmp_path);e.open(signal(),NOW);e.s['halt_reason']='DAILY_LOSS_LIMIT';e.tick(NOW)
    assert len(t.jobs)==1 and e.active()[0]['status']==10


def test_drawdown_halt_cannot_be_replaced_then_reset_as_daily_loss(tmp_path):
    e,t=engine(tmp_path);e.apply_snapshot(snapshot(nav=940000),NOW)
    assert e.s['halt_reason']=='OBSERVATION_DRAWDOWN_LIMIT'
    e.apply_snapshot(snapshot(nav=980000),NOW+pd.Timedelta(minutes=1))
    e.apply_snapshot(snapshot(nav=980000),NOW+pd.Timedelta(days=1))
    assert e.s['halt_reason']=='OBSERVATION_DRAWDOWN_LIMIT'


def cache(tmp_path,now=NOW,**patch):
    path=tmp_path/'ev/latest_quotes/DCE.m2701.json';path.parent.mkdir(parents=True,exist_ok=True)
    q=dict(quote(now),source='GM_BULK_CURRENT_POLL');q.update(patch)
    path.write_text(json.dumps(dict(symbol='DCE.m2701',config_sha256='HASH',received_at=str(now),quote=q)))
    return path


def test_latest_quote_validates_fixed_price_without_repricing(tmp_path):
    cache(tmp_path);a=dict(symbol='DCE.m2701',tick=1.,price=101.,side=1)
    r=latest_quote(tmp_path,dict(evidence_dir='ev'),'HASH',a,NOW.to_pydatetime())
    assert r['limit_price_unchanged']==101 and r['bid']==100


@pytest.mark.parametrize('patch',[dict(bid_size=0),dict(source='OTHER'),dict(tick=.5),dict(bid=105),dict(ask=110)])
def test_latest_quote_invalid_book_blocks(tmp_path,patch):
    cache(tmp_path,**patch)
    with pytest.raises(ValueError):latest_quote(tmp_path,dict(evidence_dir='ev'),'HASH',dict(symbol='DCE.m2701',tick=1.,price=101.,side=1),NOW.to_pydatetime())


@pytest.mark.parametrize('age',[-4,4])
def test_latest_quote_stale_or_future_blocks(tmp_path,age):
    cache(tmp_path,NOW+pd.Timedelta(seconds=age))
    with pytest.raises(ValueError):latest_quote(tmp_path,dict(evidence_dir='ev'),'HASH',dict(symbol='DCE.m2701',tick=1.,price=101.,side=1),NOW.to_pydatetime())


def test_cache_wrong_profile_or_adverse_price_blocks(tmp_path):
    cache(tmp_path)
    for digest,price in [('OTHER',101),('HASH',102)]:
        with pytest.raises(ValueError):latest_quote(tmp_path,dict(evidence_dir='ev'),digest,dict(symbol='DCE.m2701',tick=1.,price=price,side=1),NOW.to_pydatetime())


def test_vendor_error_detail_is_preserved_and_private_hook_redacted():
    from test_paper_notify import Opener,Response,HOOK
    from futures_trend_quant.live.paper_notify import send_once
    result=send_once(dict(webhook=HOOK), 'status',Opener(Response(json.dumps(dict(code=11232,msg='blocked '+HOOK)).encode())))
    assert result['status']=='REJECTED_VENDOR' and result['vendor_code']==11232
    assert 'blocked' in result['vendor_message'] and HOOK not in result['vendor_message']


def test_latest_quote_cannot_revive_broken_structure(tmp_path):
    cache(tmp_path,last=98)
    with pytest.raises(ValueError,match='anchor broken'):
        latest_quote(tmp_path,dict(evidence_dir='ev'),'HASH',dict(symbol='DCE.m2701',tick=1.,price=101.,side=1,anchor=99.),NOW.to_pydatetime())


def test_legacy_candidate_does_not_replay_after_expiry_or_new_run(tmp_path):
    from test_paper_notice_details import fixture,put,NOW as time0
    from tools.paper_notice_details import candidate_details,run_cursor_floor
    root,row=fixture(tmp_path);ev=root/'evidence/paper_observation_five'
    candidate_details(tmp_path,time0-timedelta(seconds=10))
    (ev/'signals.jsonl').write_text(json.dumps(row)+'\n',encoding='utf8')
    assert candidate_details(tmp_path,time0+timedelta(minutes=1))==[]
    put(ev/'run_manifest.json',dict(started_at=(time0+timedelta(seconds=1)).isoformat()))
    assert run_cursor_floor(ev,dict(cutoff=time0.isoformat()))['cutoff']==(time0+timedelta(seconds=1)).isoformat()


def test_shadow_changes_no_actual_stop_target_or_exit(tmp_path):
    e,t=engine(tmp_path);e.c['execution_policy_version']=VERSION
    at=NOW.replace(hour=13,minute=30);e.apply_snapshot(snapshot(),at);e.quote('DCE.m2701',quote(at),at);e.open(signal(at),at)
    from test_paper_execution import ack
    row=ack(e,status=3,filled=1,now=at);e.apply_snapshot(snapshot(orders=[row],positions=[position()]),at)
    m=e.s['meta']['DCE.m2701|bull'];before=(m['stop'],m['target'],m['force_exit']);t.jobs.clear()
    for n in range(1,46):
        end=at+pd.Timedelta(minutes=n)
        b=Bar(symbol='DCE.m2701',frequency='60s',bob=(end-pd.Timedelta(minutes=1)).to_pydatetime(),eob=end.to_pydatetime(),open=101,high=101.1,low=100.9,close=101,volume=1,amount=101,position=1)
        e.m1(b,end)
    assert m['shadow_risk']['time_stop_triggered']
    assert (m['stop'],m['target'],m['force_exit'])==before and not t.jobs


def test_fill_ack_before_position_snapshot_still_records_m1_stop(tmp_path):
    e,t=engine(tmp_path);e.c['execution_policy_version']=VERSION;e.open(signal(),NOW)
    from test_paper_execution import ack
    ack(e,status=3,filled=1)
    assert not e.positions()
    end=NOW+pd.Timedelta(minutes=1)
    b=Bar(symbol='DCE.m2701',frequency='60s',bob=NOW.to_pydatetime(),eob=end.to_pydatetime(),open=101,high=101,low=96,close=96,volume=1,amount=101,position=1)
    e.m1(b,end)
    assert e.s['meta']['DCE.m2701|bull']['force_exit']=='STOP_M1_TRIGGER'


def test_effective_friday_deadline_is_today_not_monday(tmp_path):
    e,t=engine(tmp_path);cal=e.calendar
    assert cal.deadline('2026-11-06').startswith('2026-11-09')
    assert cal.effective_deadline('2026-11-06','2026-11-06T10:00:00+08:00').startswith('2026-11-06T14:55')


@pytest.mark.parametrize('mutation',[None,'stale','profile','risk_halt','price','missing'])
def test_real_native_exit_refreshes_quote_after_cash_queries_without_repricing(tmp_path,monkeypatch,mutation):
    from test_paper_bridge import invoke,args,req
    from futures_trend_quant.live.paper_config import digest
    moment=NOW.to_pydatetime();current=[moment]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return current[0]
    def prepare(root,config):
        (root/'state').mkdir(exist_ok=True)
        (root/'state/execution.json').write_text(json.dumps(dict(config_sha256=digest(config),account_id=ACCOUNT,
            day='2026-11-05',pause_new=False,halt_reason='DAILY_LOSS_LIMIT' if mutation=='risk_halt' else '')))
    def account_delay():
        current[0]=moment+timedelta(seconds=4)
        if mutation=='missing':return
        event=moment if mutation=='stale' else current[0]
        q=dict(quote(pd.Timestamp(event)),source='GM_BULK_CURRENT_POLL')
        if mutation=='price':q.update(bid=98,ask=99)
        p=tmp_path/'ev/latest_quotes/DCE.m2701.json';p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(dict(config_sha256='OTHER' if mutation=='profile' else digest(tmp_path/'config.json'),
            symbol='DCE.m2701',quote=q,received_at=event.isoformat())))
    result,calls=invoke(tmp_path,monkeypatch,[req('submit',arguments=args(moment))],clock=Clock,
                       profile_extra=dict(execution_policy_version=VERSION,evidence_dir='ev',state_dir='state'),
                       before_serve=prepare,cash_hook=account_delay)
    submitted=[value for name,value in calls if name=='submit']
    if mutation is None:
        assert len(submitted)==1 and submitted[0]['price']==101
        assert result[0]['native_submit_invoked'] is True
        assert pd.Timestamp(result[0]['final_execution_quote']['event_at'])==pd.Timestamp(moment+timedelta(seconds=4))
    else:
        assert not submitted and result[0]['submit_may_have_reached_broker'] is False
