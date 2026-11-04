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
        ns=ns,index=idx,values=values,facts=facts,adv={'2026-07-01':100000.},frame=None)
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


def test_zero_volume_entry_is_cancelled():
    r=Account(fixture(zero=True),'S0').run()
    assert not r['trades'] and not r['opened'] and r['blocks']['UNVERIFIABLE_ONE_PRICE_OR_ZERO_VOLUME_ENTRY']==1


def test_fees_use_trading_day_and_are_debited_at_both_sides():
    d=fixture();cost=json.loads((ROOT/'tools/full_trade_v8/cost_reference.json').read_text(encoding='utf8'))
    r=Account(d,'S0',cost_model=cost,slip_ticks=0).run();t=r['trades'][0]
    assert t['close_today'] and t['modeled_fee']==t['lots']*5
    assert r['curve'][-1]['equity']==pytest.approx(1_000_000+t['modeled_net_pnl'])
