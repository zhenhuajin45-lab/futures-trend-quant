"""The real verdict must distinguish verified cheap, expensive and mismatched costs."""
import pytest
from copy import deepcopy
from pathlib import Path
import pandas as pd
from backtest.forward_v3 import read,write,digest
from tools.short_swing_v9.entrypoint import judgment
from tools.short_swing_v9.cost_evidence import load_cost_contract,ledger_cost_coverage,decision_cost_status
from tools.short_swing_v9.independent import execute,ROOT
from test_short_swing_v9 import synthetic_bundle


def passing_inputs():
    reg=dict(maximum_m1_close_drawdown=.15,capacity=dict(minimum_score_days=60,minimum_closed_trades_each_arm=100))
    s={x:{g:dict(closed_trades=120,mean_net_r=.1,total_marked_pnl=10000,maximum_m1_close_drawdown_pct=.12) for g in ('S0','ORDINARY')} for x in ('REFERENCE_1T','REFERENCE_2T')}
    ci={x:dict(blocks={b:dict(S0=dict(mean_net_r_95ci=[.02,.2])) for b in ('5','10')}) for x in s}
    c={x:dict(S0=dict(without_top_product=1000,without_top5=1000)) for x in s}
    return s,ci,c,reg


def assessment(ticks=1.,fee_match=True):
    return dict(status='VERIFIED_WITHIN_BUDGET',max_observed_slippage_ticks_per_side=ticks,
        fee_model_matches=fee_match,ledger_coverage_complete=True,evidence_sha256='a'*64,model_sha256='b'*64)


def test_verified_three_ticks_cannot_pass_even_if_all_other_metrics_pass():
    assert judgment(*passing_inputs(),assessment(3),82)['status']=='INSUFFICIENT_COST_MARGIN'


def test_verified_fees_differ_from_model_cannot_certify_reference_ledger():
    assert judgment(*passing_inputs(),assessment(fee_match=False),82)['status']=='COST_MODEL_REPLAY_REQUIRED'


@pytest.mark.parametrize('claim',[True,False,None,{},dict(fee_schedule_verified=True,quote_size_and_exit_costs_verified=True)])
def test_legacy_boolean_or_incomplete_claim_stays_pending(claim):
    assert judgment(*passing_inputs(),claim,82)['status']=='ECONOMICS_PENDING_COST_VALIDATION'


def attach_costs(protocol,bundle,data,ticks=1):
    model_path=ROOT/'tools/full_trade_v8/cost_reference.json';model=read(model_path)
    original=bundle/'cost_original.txt';original.write_text('SYNTHETIC FEE AND L1 ORIGINAL - NOT REAL EVIDENCE',encoding='utf8')
    quotes=[]
    for offset,direction,t in [('open','bull','2025-09-01T09:15:00+08:00'),('today','bull','2025-09-01T09:20:00+08:00')]:
        buy=offset=='open';bid=100-ticks if not buy else 100;ask=100+ticks if buy else 100
        quotes.append(dict(source=original.name,symbol='DCE.m.TEST',csymbol='DCE.M',position_direction=direction,
            offset=offset,lots=1,reference_price=100,bid=bid,ask=ask,bid_size=10,ask_size=10,
            executable_price=ask if buy else bid,tick_size=1,trading_date='2025-09-01',
            decision_at=t,quote_at=t,received_at=t,session_start='2025-09-01T09:00:00+08:00',session_end='2025-09-01T09:30:00+08:00'))
    evidence=dict(schema_version=1,owner='synthetic-data',reviewer='synthetic-review',fee_schedule_verified=True,
        quote_size_and_exit_costs_verified=True,modeled_cost_sha256=digest(model_path),source_hashes={original.name:digest(original)},
        fee_periods=[dict(source=original.name,symbol='DCE.m.TEST',csymbol='DCE.M',start='2025-09-01',end='2025-09-02',
            rates=model['products']['M'],broker_add_per_lot_per_side=model['broker_add_per_lot_per_side'])],quote_samples=quotes)
    write(bundle/'cost_evidence.json',evidence);lock=read(bundle/'input_lock.json')
    lock.update(costs_certified=True,cost_evidence_sha256=digest(bundle/'cost_evidence.json'))
    lock['source_hashes'].update(evidence['source_hashes']);write(bundle/'input_lock.json',lock)
    return evidence


def update_evidence(bundle,evidence):
    write(bundle/'cost_evidence.json',evidence);lock=read(bundle/'input_lock.json');lock['cost_evidence_sha256']=digest(bundle/'cost_evidence.json');write(bundle/'input_lock.json',lock)


def load(protocol,bundle,data):
    return load_cost_contract(bundle,read(bundle/'input_lock.json'),read(protocol),data,ROOT/'tools/full_trade_v8/cost_reference.json')


def test_actual_input_gate_rejects_three_ticks_before_output_or_test_consumption(tmp_path):
    protocol,bundle,data=synthetic_bundle(tmp_path);attach_costs(protocol,bundle,data,3)
    out=tmp_path/'never_created';registry=tmp_path/'no_registry'
    with pytest.raises(ValueError,match='INSUFFICIENT_COST_MARGIN'):execute(protocol,bundle,out,registry,True)
    assert not out.exists() and not registry.exists()


def test_actual_input_gate_rejects_different_fees_before_replaying_reference_account(tmp_path):
    protocol,bundle,data=synthetic_bundle(tmp_path);e=attach_costs(protocol,bundle,data)
    e['fee_periods'][0]['rates']['open_per_lot']+=1;update_evidence(bundle,e)
    with pytest.raises(ValueError,match='COST_MODEL_REPLAY_REQUIRED'):execute(protocol,bundle,tmp_path/'never',synthetic=True)
    assert not (tmp_path/'never').exists()


def test_hashed_original_tampering_rejected(tmp_path):
    protocol,bundle,data=synthetic_bundle(tmp_path);attach_costs(protocol,bundle,data)
    (bundle/'cost_original.txt').write_text('modified',encoding='utf8')
    with pytest.raises(ValueError,match='cost original'):load(protocol,bundle,data)


@pytest.mark.parametrize('change',[
    dict(ask_size=0),dict(position_direction='bear'),dict(received_at='2025-09-01T09:15:01+08:00'),
    dict(quote_at='2025-09-01T09:14:58+08:00'),dict(trading_date='2025-08-29'),dict(tick_size=2),dict(lots=20)])
def test_unknown_size_direction_clock_or_tick_cannot_be_certified(tmp_path,change):
    protocol,bundle,data=synthetic_bundle(tmp_path);e=attach_costs(protocol,bundle,data)
    e['quote_samples'][0].update(change);update_evidence(bundle,e);a,_=load(protocol,bundle,data)
    assert decision_cost_status(a)=='ECONOMICS_PENDING_COST_VALIDATION'


def test_fee_scope_must_cover_selected_contract_dates(tmp_path):
    protocol,bundle,data=synthetic_bundle(tmp_path);e=attach_costs(protocol,bundle,data)
    e['fee_periods'][0]['end']='2025-09-01';update_evidence(bundle,e);a,_=load(protocol,bundle,data)
    assert a['status']=='ECONOMICS_PENDING_COST_VALIDATION'


def test_verified_costs_flow_from_sources_through_execution_and_verdict(tmp_path):
    protocol,bundle,data=synthetic_bundle(tmp_path);attach_costs(protocol,bundle,data);a,contract=load(protocol,bundle,data)
    row=dict(symbol='DCE.m.TEST',csymbol='DCE.M',direction='bull',lots=1,entry_bob='2025-09-01T09:15:00+08:00',
        entry_day='2025-09-01',entry_price=101.,exit_time='2025-09-01T09:20:00+08:00',exit_day='2025-09-01',
        close_today=True,exit_price=99.,raw_exit_proxy=100.,modeled_fee=5.)
    ledger=dict(trades=[row],opened=[]);coverage=ledger_cost_coverage(ledger,contract,data,1)
    assert coverage['complete'] and coverage['execution_sides']==2
    a['ledger_coverage_complete']=coverage['complete']
    assert judgment(*passing_inputs(),a,82)['status']=='ECONOMIC_CRITERIA_PASS_EXECUTION_REVIEW_STILL_REQUIRED'
    row['lots']=2
    assert not ledger_cost_coverage(ledger,contract,data,1)['complete']


def test_quote_coverage_checked_on_final_actual_runner_ledger(tmp_path):
    protocol,bundle,data=synthetic_bundle(tmp_path);attach_costs(protocol,bundle,data)
    r=execute(protocol,bundle,tmp_path/'fresh',synthetic=True)
    assert r['modeled_cost_sha256']==digest(ROOT/'tools/full_trade_v8/cost_reference.json')
    assert not r['costs_certified'] and not r['cost_assessment']['ledger_coverage_complete']
    assert judgment(*passing_inputs(),r['cost_assessment'],82)['status']=='ECONOMICS_PENDING_COST_VALIDATION'
