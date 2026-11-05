from copy import deepcopy
from pathlib import Path
import json
import pandas as pd
import pytest
from test_paper_execution import calendar,configuration,FakeTransport,signal,quote,snapshot,position,ack,NOW
from futures_trend_quant.live.paper_products import product_of,close_effect,bucket_available,FIVE_POOL
from futures_trend_quant.live.paper_calendar import PaperCalendar,PortfolioCalendar
from futures_trend_quant.live.paper_five import contract_record
from futures_trend_quant.live.paper_broker import PaperEngine
from futures_trend_quant.live.paper_config import load_paper_config,ROOT,digest


@pytest.mark.parametrize('symbol,cs',[('DCE.m2701','DCE.M'),('SHFE.au2612','SHFE.AU'),('DCE.eg2701','DCE.EG'),('DCE.jd2612','DCE.JD'),('CZCE.CJ701','CZCE.CJ')])
def test_exact_actual_contract_identity(symbol,cs):assert product_of(symbol)==cs


@pytest.mark.parametrize('symbol',['DCE.M','SHFE.AU','CZCE.MA701','CZCE.CJ2701','DCE.M2701','SHFE.au9999BAD'])
def test_unregistered_or_continuous_contract_cannot_trade(symbol):
    with pytest.raises(ValueError):product_of(symbol)


def au_calendar():
    c=calendar()
    return PaperCalendar(c.dates,[dict(start='21:00',end='2:30'),dict(start='9:00',end='10:15'),dict(start='10:30',end='11:30'),dict(start='13:30',end='15:00')],'SHFE.AU')


def test_gold_night_calendar_and_friday_open_blackout_keep_risk_exit_window():
    c=au_calendar();t=pd.Timestamp('2026-11-07T01:00:00+08:00')
    assert c.market_open(t) and c.day(t)=='2026-11-09' and c.entry_block(t)=='PRE_LONG_CLOSURE_NO_REOPEN'
    m=dict(csymbol='SHFE.AU',symbol='SHFE.au2612',spec=dict(price_tick=.02,multiplier=1000,margin_ratio=.16))
    r=contract_record(m,c);timestamp=t.timestamp()
    assert any(a<=timestamp<b for a,b in r['trade_windows'])
    assert not any(a<=timestamp<b for a,b in r['entry_windows'])
    assert c.daily_counts['2026-11-06']==555


def test_day_only_products_do_not_borrow_other_products_night_session():
    c=PaperCalendar(calendar().dates,[dict(start='9:00',end='10:15'),dict(start='10:30',end='11:30'),dict(start='13:30',end='15:00')],'DCE.JD')
    p=PortfolioCalendar();p.register('SHFE.au2612',au_calendar());p.register('DCE.jd2612',c)
    t='2026-11-05T23:30:00+08:00'
    assert p.for_symbol('SHFE.au2612').market_open(t)
    assert not p.for_symbol('DCE.jd2612').market_open(t)
    assert c.daily_counts['2026-11-06']==225


def test_shfe_close_today_yesterday_are_explicit_and_unknown_is_not_guessed():
    assert close_effect('SHFE.au2612',dict(available=1,available_today=1,volume_today=1))==3
    assert close_effect('SHFE.au2612',dict(available=1,available_today=0,volume_today=0))==4
    with pytest.raises(ValueError):close_effect('SHFE.au2612',dict(available=1))
    assert close_effect('DCE.m2701',dict(available=1))==2
    assert bucket_available(dict(available=2,available_today=1),3)==1
    assert bucket_available(dict(available=2,available_today=1),4)==1


def test_close_today_intent_blocks_a_duplicate_close_as_well_as_generic_close(tmp_path):
    p=PortfolioCalendar();p.register('DCE.m2701',calendar());p.register('SHFE.au2612',au_calendar())
    c=configuration(tmp_path);c['universe']=list(FIVE_POOL);e=PaperEngine(c,p,FakeTransport())
    e.s['intents']['C']=dict(id='C',symbol='SHFE.au2612',effect=3,status=1)
    assert len(e.active('SHFE.au2612',effect=2))==1


def test_wrong_known_order_identity_cannot_cross_products(tmp_path):
    c=configuration(tmp_path);t=FakeTransport();e=PaperEngine(c,calendar(),t);e.apply_snapshot(snapshot(),NOW);e.s['connectivity_passed']=True
    e.quote('DCE.m2701',quote(),NOW);e.open(signal(),NOW);row=ack(e)
    with pytest.raises(ValueError,match='contradiction'):e.on_order(dict(row,symbol='DCE.eg2701'),NOW)


def test_new_pool_configuration_is_exact_and_keeps_original_core_fingerprint(tmp_path):
    import shutil
    (tmp_path/'config').mkdir();shutil.copytree(ROOT/'futures_trend_quant/core',tmp_path/'futures_trend_quant/core')
    (tmp_path/'tools/short_swing_v9').mkdir(parents=True);shutil.copyfile(ROOT/'tools/short_swing_v9/policy.py',tmp_path/'tools/short_swing_v9/policy.py')
    shutil.copyfile(ROOT/'config/strategy.json',tmp_path/'config/strategy.json')
    proof=tmp_path/'config/paper_account.local.json';proof.write_text(json.dumps(dict(account_id='00000000-0000-0000-0000-000000000001',endpoint='api.myquant.cn:9000',source='GM_OFFICIAL_SIM_ACCOUNT_CASH_ID_MATCH',user_designated_futures_sim=True)))
    c=json.loads((ROOT/'config/paper_observation_five.example.json').read_text());c['account_binding_sha256']=digest(proof)
    path=tmp_path/'config/paper_observation_five.local.json';path.write_text(json.dumps(c))
    assert load_paper_config(path,root=tmp_path)['universe']==list(FIVE_POOL)
    c['universe'].append('CZCE.MA');path.write_text(json.dumps(c))
    with pytest.raises(ValueError,match='unregistered'):load_paper_config(path,root=tmp_path)
