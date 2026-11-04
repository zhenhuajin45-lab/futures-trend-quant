from pathlib import Path
from copy import deepcopy
import pickle,json
import numpy as np
import pandas as pd
import pytest
from tools.full_trade_v8.engine import Account,MINUTE
from tools.short_swing_v9.account import ShortSwingAccount
from tools.short_swing_v9.independent import execute,REQUIRED_ENGINE_FILES,ROOT
from tools.historical_strategy.reproduce import canonical_digest
from backtest.forward_v3 import read,write,digest


def fact(identity='first',accepted=True):
    return dict(eligible=True,core=dict(close=100.,atr14=1.,trend_decision=dict(confirmed=False,state=dict(trend_state='bull')),
        entry_evaluation=dict(accepted=accepted,path='D',evidence_id=identity,formed_paths=['D'],anchor=dict(anchor_price=99.))))


def payload(times,days,facts):
    times=pd.DatetimeIndex(times);ns=times.as_unit('ns').asi8;symbol='DCE.m.TEST'
    leg=dict(cs='DCE.M',symbol=symbol,expected=ns,ns=ns,days=days,daymap=dict(zip(ns,days)),index=dict(zip(ns,range(len(ns)))),
        values=np.tile([100.,100.1,99.9,100.,100.],(len(ns),1)),facts=facts,
        adv={d:100000. for d in days},selection={d:symbol for d in days},frame=None)
    return dict(config=read(ROOT/'config/strategy.json'),fees=read(ROOT/'config/fees.json'),
        clocks={'DCE.M':dict(ns=ns,days=days)},legs={symbol:leg})


def sessions():
    dates=['2026-06-24','2026-06-25','2026-06-26','2026-06-29','2026-06-30'];points=[];days=[]
    for i,day in enumerate(dates):
        if i:
            previous=dates[i-1]
            night=pd.date_range(previous+'T21:01:00+08:00',previous+'T23:00:00+08:00',freq='min')
            points.extend(night);days.extend([day]*len(night))
        for begin,end in [('09:01','10:15'),('10:31','11:30'),('13:31','15:00')]:
            part=pd.date_range(day+'T'+begin+':00+08:00',day+'T'+end+':00+08:00',freq='min')
            points.extend(part);days.extend([day]*len(part))
    return points,days


def data_at(time):
    points,days=sessions();return payload(points,days,{pd.Timestamp(time).value:fact()})


POLICY=dict(buffer_minutes=5,weekend_mode='DAY_CLOSE')


def test_no_policy_is_exact_frozen_account():
    d=data_at('2026-06-24T10:00:00+08:00')
    assert ShortSwingAccount(d,'S0').run()==Account(d,'S0').run()


def test_next_trading_day_1455_exit():
    r=ShortSwingAccount(data_at('2026-06-24T10:00:00+08:00'),'S0',policy=POLICY).run()
    assert r['trades'][0]['exit_time']=='2026-06-25T14:55:00+08:00'
    assert r['trades'][0]['reason']=='NEXT_TRADING_DAY_DEADLINE'


def test_friday_daytime_exit_no_night_reopen_monday_resume():
    d=data_at('2026-06-26T10:00:00+08:00');l=d['legs']['DCE.m.TEST']
    l['facts'][pd.Timestamp('2026-06-26T21:15:00+08:00').value]=fact('friday-night')
    l['facts'][pd.Timestamp('2026-06-29T09:15:00+08:00').value]=fact('monday')
    r=ShortSwingAccount(d,'S0',policy=POLICY).run()
    assert r['trades'][0]['exit_time']=='2026-06-26T14:55:00+08:00'
    assert [x['time'] for x in r['events']]==['2026-06-26T10:00:00+08:00','2026-06-29T09:15:00+08:00']


@pytest.mark.parametrize('block',['LIMIT_DOWN_NO_BUYER','NO_QUOTE','UNFILLED'])
def test_blocked_exit_retains_exposure_until_first_model_fill(block):
    d=data_at('2026-06-24T10:00:00+08:00');deadline=pd.Timestamp('2026-06-25T14:55:00+08:00').value
    d['execution_snapshots']={('DCE.m.TEST',deadline+i*MINUTE):dict(blocked_reason=block) for i in range(2)}
    d['execution_snapshots'][('DCE.m.TEST',deadline+2*MINUTE)]=dict(blocked_reason=None)
    r=ShortSwingAccount(d,'S0',policy=POLICY).run();t=r['trades'][0]
    assert t['exit_time']=='2026-06-25T14:57:00+08:00' and t['blocked_attempts']==2
    assert all(x['lots_remaining']>0 for x in r['exit_attempts'])
    assert t['first_blocked_exit_at']=='2026-06-25T14:55:00+08:00'


def test_missing_deadline_m1_preserves_position_and_records_delay():
    d=data_at('2026-06-24T10:00:00+08:00');leg=d['legs']['DCE.m.TEST']
    del leg['index'][pd.Timestamp('2026-06-25T14:56:00+08:00').value]
    r=ShortSwingAccount(d,'S0',policy=POLICY).run();t=r['trades'][0]
    assert t['had_data_gap'] and t['exit_time']=='2026-06-25T14:56:00+08:00'
    assert t['discipline_requested_at']=='2026-06-25T14:55:00+08:00'


def test_end_unclosed_is_marked_not_artificially_liquidated():
    r=ShortSwingAccount(data_at('2026-06-30T14:30:00+08:00'),'S0',policy=POLICY).run()
    assert not r['trades'] and len(r['opened'])==1 and r['opened'][0]['discipline_deadline'] is None


def test_later_prices_do_not_change_earlier_weekend_exit():
    d=data_at('2026-06-26T10:00:00+08:00');other=deepcopy(d);l=other['legs']['DCE.m.TEST']
    for t,i in l['index'].items():
        if t>=pd.Timestamp('2026-06-29T09:01:00+08:00').value:l['values'][i]=[500,600,400,500,0]
    assert ShortSwingAccount(d,'S0',policy=POLICY).run()['trades']==ShortSwingAccount(other,'S0',policy=POLICY).run()['trades']


def test_midweek_long_holiday_exit_and_first_postholiday_night_resume():
    points=[];days=[]
    for day in ('2025-09-30','2025-10-09'):
        if day=='2025-10-09':
            part=pd.date_range('2025-10-08T21:01:00+08:00','2025-10-08T23:00:00+08:00',freq='min')
            points.extend(part);days.extend([day]*len(part))
        for start,end in [('09:01','10:15'),('10:31','11:30'),('13:31','15:00')]:
            part=pd.date_range(day+'T'+start+':00+08:00',day+'T'+end+':00+08:00',freq='min')
            points.extend(part);days.extend([day]*len(part))
    d=payload(points,days,{pd.Timestamp('2025-09-30T10:00:00+08:00').value:fact(),
        pd.Timestamp('2025-10-08T21:15:00+08:00').value:fact('after-holiday')})
    r=ShortSwingAccount(d,'S0',policy=POLICY).run()
    assert r['trades'][0]['exit_time']=='2025-09-30T14:55:00+08:00'
    assert r['events'][-1]['time']=='2025-10-08T21:15:00+08:00'


def test_unfilled_friday_order_keeps_weekend_exposure_on_book():
    d=data_at('2026-06-26T10:00:00+08:00');l=d['legs']['DCE.m.TEST']
    start=pd.Timestamp('2026-06-26T14:55:00+08:00').value;resume=pd.Timestamp('2026-06-29T09:00:00+08:00').value
    d['execution_snapshots']={('DCE.m.TEST',int(t)-MINUTE):dict(blocked_reason='UNFILLED') for t in l['expected'] if start<=int(t)-MINUTE<resume}
    r=ShortSwingAccount(d,'S0',policy=POLICY).run()
    assert r['trades'][0]['exit_time']=='2026-06-29T09:00:00+08:00'
    assert next(c for c in r['curve'] if c['eob']=='2026-06-26T23:00:00+08:00')['open_positions']==1


def synthetic_bundle(tmp_path,zero=False):
    warm=pd.date_range('2025-08-29T14:31:00+08:00',periods=30,freq='min')
    night=pd.date_range('2025-08-29T21:01:00+08:00',periods=30,freq='min')
    one=pd.date_range('2025-09-01T09:01:00+08:00',periods=30,freq='min')
    two=pd.date_range('2025-09-02T09:01:00+08:00',periods=30,freq='min')
    points=list(warm)+list(night)+list(one)+list(two);days=['2025-08-29']*30+['2025-09-01']*60+['2025-09-02']*30
    facts={t.value:fact(str(t),True) for t in points if t.minute in (0,15,30,45)}
    if zero:
        for row in facts.values():row['core']['trend_decision']['state']['trend_state']='none'
    data=payload(points,days,facts);bundle=tmp_path/'input';bundle.mkdir()
    protocol=tmp_path/'protocol.json';reg=dict(study='SYNTHETIC_V9',score_trade_dates=['2025-09-01','2025-09-02'],
        maximum_m1_close_drawdown=.15,candidate_universe=['DCE.M'],engine_hashes={p:canonical_digest(ROOT/p) for p in REQUIRED_ENGINE_FILES},
        source_admission=dict(minimum_source_qualified_products=1,strict_complete_same_leg_prewarm_m15=2,
        selected_expected_minute_coverage_per_product_minimum=.95,eligible_selected_m15_coverage_per_product_minimum=.80))
    write(protocol,reg)
    write(bundle/'calendar.json',dict(products={'DCE.M':[dict(eob=t.isoformat(),trading_date=d) for t,d in zip(points,days)]}))
    with (bundle/'prepared.pkl').open('wb') as f:pickle.dump(data,f)
    lock=dict(protocol_sha256=canonical_digest(protocol),score_trade_dates=reg['score_trade_dates'],data_kind='SYNTHETIC',
        source_hashes={'calendar.json':digest(bundle/'calendar.json')},calendar_sha256=digest(bundle/'calendar.json'),
        prepared_sha256=digest(bundle/'prepared.pkl'),engine_hashes=reg['engine_hashes'])
    write(bundle/'input_lock.json',lock);return protocol,bundle,data


def test_isolated_runner_night_start_empty_account_and_end_open(tmp_path):
    protocol,bundle,data=synthetic_bundle(tmp_path);out=tmp_path/'fresh_output'
    old={p:digest(p) for p in (ROOT/'reports').glob('v8_*.json')}
    result=execute(protocol,bundle,out,synthetic=True)
    assert result['status']=='SYNTHETIC_SMOKE_ONLY' and not result['unique_real_test_consumed']
    curve=pd.read_csv(out/'GROSS_S0_curve.csv');events=pd.read_csv(out/'GROSS_S0_events.csv')
    assert curve.iloc[0].trading_date=='2025-09-01' and curve.iloc[0].open_positions==0
    assert events.iloc[0].time=='2025-08-29T21:15:00+08:00'
    assert len(pd.read_csv(out/'GROSS_S0_opened.csv'))>0
    assert all(digest(p)==sha for p,sha in old.items())


def test_complete_market_zero_signals_is_valid_synthetic(tmp_path):
    protocol,bundle,_=synthetic_bundle(tmp_path,True);out=tmp_path/'zero'
    r=execute(protocol,bundle,out,synthetic=True)
    assert r['groups']['GROSS']['S0']['closed_trades']==0
    assert len(pd.read_csv(out/'GROSS_S0_events.csv'))==0


def test_empty_replay_fails_before_output_or_success(tmp_path):
    protocol,bundle,data=synthetic_bundle(tmp_path);data['legs']['DCE.m.TEST']['facts']={}
    with (bundle/'prepared.pkl').open('wb') as f:pickle.dump(data,f)
    lock=read(bundle/'input_lock.json');lock['prepared_sha256']=digest(bundle/'prepared.pkl');write(bundle/'input_lock.json',lock)
    out=tmp_path/'should_not_exist'
    with pytest.raises(ValueError,match='empty raw or replay'):execute(protocol,bundle,out,synthetic=True)
    assert not out.exists()


def test_changed_registration_dates_refused(tmp_path):
    protocol,bundle,_=synthetic_bundle(tmp_path);lock=read(bundle/'input_lock.json');lock['score_trade_dates']=['2025-09-01'];write(bundle/'input_lock.json',lock)
    with pytest.raises(ValueError,match='date set mismatch'):execute(protocol,bundle,tmp_path/'bad',synthetic=True)


def test_new_output_inside_any_legacy_reports_refused(tmp_path):
    protocol,bundle,_=synthetic_bundle(tmp_path)
    with pytest.raises(ValueError,match='protected project'):execute(protocol,bundle,ROOT.parent/'ftq-v3/reports/no_mixing',synthetic=True)


def test_one_minute_per_m15_calendar_cannot_manufacture_prewarm(tmp_path):
    from tools.short_swing_v9.independent import scoring_view
    protocol,bundle,data=synthetic_bundle(tmp_path);clock=data['clocks']['DCE.M'];mask=np.arange(len(clock['ns']))%15==14
    clock['ns']=clock['ns'][mask];clock['days']=np.array(clock['days'])[mask].tolist()
    with pytest.raises(ValueError,match='incomplete M15'):scoring_view(data,read(protocol),True)
