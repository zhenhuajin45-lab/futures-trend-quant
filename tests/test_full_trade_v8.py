import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from tools.full_trade_v8.engine import Account,intrabar_exit

ROOT=Path(__file__).resolve().parents[1]


def fixture(missing=None,flip=True,zero=False):
    times=pd.date_range('2026-07-01T09:01:00+08:00',periods=20,freq='min');ns=times.as_unit('ns').asi8
    values=np.tile([100.,101.,99.,100.,100.],(20,1));values[17:]=[102.,103.,101.,102.,100.]
    if zero:values[15,4]=0
    facts={}
    for i,state,confirmed in [(14,'bull',False),(16,'none' if flip else 'bull',flip)]:
        facts[int(ns[i])]=dict(eligible=True,core=dict(close=100.,atr14=1.,trend_decision=dict(confirmed=confirmed,state=dict(trend_state=state)),
            entry_evaluation=dict(accepted=True,path='D',evidence_id='D|first',formed_paths=['D'],anchor=dict(anchor_price=99.))))
    idx={int(t):i for i,t in enumerate(ns) if i!=missing}
    leg=dict(cs='DCE.M',symbol='DCE.m.TEST',expected=ns,daymap={int(t):'2026-07-01' for t in ns},days=['2026-07-01']*20,
        ns=ns,index=idx,values=values,facts=facts,adv={'2026-07-01':100000.},frame=None,
        selection={'2026-07-01':'DCE.m.TEST'})
    return dict(config=json.loads((ROOT/'config/strategy.json').read_text(encoding='utf8')),fees=json.loads((ROOT/'config/fees.json').read_text(encoding='utf8')),
        clocks={'DCE.M':dict(ns=ns,days=leg['days'])},legs={leg['symbol']:leg})


def test_long_gap_stop_uses_open_not_stop():
    assert intrabar_exit(1,98,104,[95,96,94,95,100])[:2]==(95.,'STOP_GAP')


def test_short_gap_stop_uses_open_not_stop():
    assert intrabar_exit(-1,102,96,[105,106,104,105,100])[:2]==(105.,'STOP_GAP')


def test_both_touch_is_stop_first():
    assert intrabar_exit(1,98,104,[100,105,97,102,100])==(98.,'STOP',True)


def test_signal_then_next_m1_open_then_trend_exit_next_open():
    r=Account(fixture(),'S0').run();trade=r['trades'][0]
    assert trade['entry_bob']=='2026-07-01T09:15:00+08:00'
    assert trade['exit_time']=='2026-07-01T09:17:00+08:00' and trade['reason']=='TREND_EXIT'
    assert trade['entry_price']==100 and trade['exit_price']==102


def test_missing_cross_position_is_not_zero_or_dropped():
    d=fixture(missing=16,flip=False);d['legs']['DCE.m.TEST']['values'][17:]=[95,96,94,95,100]
    r=Account(d,'S0').run();t=r['trades'][0]
    assert t['had_data_gap'] and t['reason']=='DATA_GAP_NEXT_OBSERVED_OPEN' and t['gross_pnl']<0


def test_end_open_is_valued_without_forced_fake_fill():
    r=Account(fixture(flip=False),'S0').run()
    assert len(r['opened'])==1 and not r['trades'] and r['opened'][0]['unrealized_pnl']>0


def test_future_zero_volume_is_annotation_not_hindsight_fill_filter():
    r=Account(fixture(zero=True),'S0').run()
    assert len(r['trades'])==1 and r['trades'][0]['entry_bar_zero_volume']


def test_fees_use_trading_day_and_are_debited_at_both_sides():
    d=fixture();cost=json.loads((ROOT/'tools/full_trade_v8/cost_reference.json').read_text(encoding='utf8'))
    r=Account(d,'S0',cost_model=cost,slip_ticks=0).run();t=r['trades'][0]
    assert t['close_today'] and t['modeled_fee']==t['lots']*5
    assert r['curve'][-1]['equity']==pytest.approx(1_000_000+t['modeled_net_pnl'])


def test_intraminute_profit_cannot_finance_same_minute_earlier_open():
    from copy import deepcopy
    from tools.full_trade_v8.engine import Holding,MINUTE
    d=fixture();original=d['legs']['DCE.m.TEST'];d['legs']={}
    symbols=[('DCE.M','DCE.m.TEST'),('CZCE.CF','CZCE.CF.TEST'),('CZCE.SA','CZCE.SA.TEST'),
        ('SHFE.AG','SHFE.ag.TEST'),('DCE.LH','DCE.lh.TEST')]
    for cs,symbol in symbols:
        leg=deepcopy(original);leg.update(cs=cs,symbol=symbol,facts={});leg['values'][0]=[100,105,99,100,100]
        d['legs'][symbol]=leg
    account=Account(d,'S0');t=int(original['expected'][0])
    for i,(cs,symbol) in enumerate(symbols):
        account.positions.append(Holding(i,symbol,cs,'bull',1,1,100,98,104,2,10,1,t-2*MINUTE,'2026-06-30','D','prior'+str(i),t-3*MINUTE))
        account.marks[symbol]=100;account.mark_times[symbol]=t-2*MINUTE
    account.pending['DCE.m.TEST']=dict(at=t,signal=t-MINUTE,cs='DCE.M',direction='bull',stop=98,atr=1,path='D',evidence='new',index=0)
    result=account.run()
    assert result['blocks']['PORTFOLIO_POSITION_CAP']==1
    assert not result['events'] and len(result['trades'])==5


def test_change_after_open_cannot_change_that_open_entry():
    a=fixture();b=fixture();b['legs']['DCE.m.TEST']['values'][15]=[100,120,80,110,0]
    first=Account(a,'S0').run()['events'][0];second=Account(b,'S0').run()['events'][0]
    assert first==second


def test_new_position_cannot_overshoot_remaining_daily_product_risk():
    d=fixture();a=Account(d,'S0');a.day='2026-07-01';a.day_realized['DCE.M']=-19500
    result=a.run();event=result['events'][0]
    assert event['initial_risk']<=500 and event['product_risk_room_before']==500


def test_portfolio_loss_plus_new_risk_cannot_overshoot_daily_limit():
    d=fixture();a=Account(d,'S0');a.day='2026-07-01';a.cash=940500
    result=a.run();event=result['events'][0]
    assert event['initial_risk']<=500 and event['portfolio_risk_room_before']==500


def test_short_fill_slippage_and_fees_reserve_actual_marked_margin():
    d=fixture();leg=d['legs']['DCE.m.TEST'];leg['values'][:]=[100,101,99,100,100]
    leg['facts'][int(leg['expected'][14])]['core'].update(close=100.,atr14=.01)
    core=leg['facts'][int(leg['expected'][14])]['core'];core['trend_decision']['state']['trend_state']='bear'
    core['entry_evaluation']['anchor']['anchor_price']=100.02
    leg['adv']['2026-07-01']=100_000_000
    d['fees']['products']['M']['price_tick']=.01
    costs=json.loads((ROOT/'tools/full_trade_v8/cost_reference.json').read_text(encoding='utf8'))
    account=Account(d,'S0',cost_model=costs,slip_ticks=1)
    event=account.run()['events'][0]
    assert event['margin_after']<=event['equity_after']*.8+1e-6
    assert event['initial_risk']+event['entry_debit']<=event['product_risk_room_before']+1e-6


@pytest.mark.parametrize('group', ['S0', 'ORDINARY'])
@pytest.mark.parametrize('next_selected', ['DCE.m.NEW', None])
def test_pending_open_must_still_be_selected_on_fill_day(group, next_selected):
    data = fixture()
    leg = data['legs']['DCE.m.TEST']
    signal = leg['facts'][int(leg['expected'][14])]
    times = pd.DatetimeIndex(['2026-07-20T15:00:00+08:00', '2026-07-20T21:01:00+08:00'])
    ns = times.as_unit('ns').asi8
    day, night = '2026-07-20', '2026-07-21'
    data['clocks'][leg['cs']] = dict(ns=ns, days=[day, night])
    leg.update(expected=ns, ns=ns, daymap=dict(zip(ns, (day, night))), index=dict(zip(ns, (0, 1))),
               facts={ns[0]: signal}, selection={day: leg['symbol'], night: next_selected},
               adv={day: 100000, night: 100000})
    account = Account(data, group)
    result = account.run()
    assert not result['events'] and not result['opened'] and not result['trades']
    assert result['blocks']['ENTRY_LEG_NOT_SELECTED_AT_FILL'] == 1
    assert account.cash == 1_000_000 and not account.used and not account.last_fill and not account.pending


@pytest.mark.parametrize('group', ['S0', 'ORDINARY'])
@pytest.mark.parametrize('next_selected', ['DCE.m.NEW', None])
def test_old_leg_exit_survives_fill_day_selection_change(group, next_selected):
    from copy import deepcopy
    data=fixture();leg=data['legs']['DCE.m.TEST']
    signal=deepcopy(leg['facts'][int(leg['expected'][14])])
    end=deepcopy(signal);end['eligible']=False
    end['core']['trend_decision']=dict(confirmed=True,state=dict(trend_state='none'))
    times=pd.DatetimeIndex(['2026-07-20T14:45:00+08:00','2026-07-20T14:46:00+08:00',
        '2026-07-20T15:00:00+08:00','2026-07-20T21:01:00+08:00'])
    ns=times.as_unit('ns').asi8;days=['2026-07-20']*3+['2026-07-21']
    leg.update(expected=ns,ns=ns,days=days,daymap=dict(zip(ns,days)),index=dict(zip(ns,range(4))),
        values=np.array([[100,101,99,100,100]]*3+[[102,103,101,102,100]],float),
        facts={ns[0]:signal,ns[2]:end},selection={days[0]:leg['symbol'],days[-1]:next_selected},
        adv={day:100000 for day in days})
    data['clocks'][leg['cs']]=dict(ns=ns,days=days)
    account=Account(data,group);result=account.run()
    assert len(result['events'])==len(result['trades'])==1 and not result['opened']
    assert result['trades'][0]['reason']=='TREND_EXIT'
    assert result['trades'][0]['exit_time']=='2026-07-20T21:00:00+08:00'
