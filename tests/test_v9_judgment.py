from copy import deepcopy
import numpy as np
from tools.short_swing_v9.entrypoint import judgment,run
from tools.short_swing_v9.metrics import block_intervals
from test_v10_cost_gate import assessment
import pytest


def inputs():
    reg=dict(score_trade_dates=[str(i) for i in range(82)],maximum_m1_close_drawdown=.15,
        capacity=dict(minimum_score_days=60,minimum_closed_trades_each_arm=100))
    s={x:{g:dict(closed_trades=120,mean_net_r=.1,total_marked_pnl=10000,maximum_m1_close_drawdown_pct=.12) for g in ('S0','ORDINARY')}
        for x in ('REFERENCE_1T','REFERENCE_2T')}
    ci={x:dict(blocks={b:dict(S0=dict(mean_net_r_95ci=[.02,.2])) for b in ('5','10')}) for x in s}
    c={x:dict(S0=dict(without_top_product=1000,without_top5=1000)) for x in s}
    return s,ci,c,reg


def test_reference_costs_cannot_pass_economic_qualification():
    s,ci,c,r=inputs();assert judgment(s,ci,c,r,False,82)['status']=='ECONOMICS_PENDING_COST_VALIDATION'


def test_positive_total_cannot_override_negative_mean_r():
    s,ci,c,r=inputs();s['REFERENCE_2T']['S0']['mean_net_r']=-.01
    assert judgment(s,ci,c,r,assessment(),82)['status']=='INSUFFICIENT_EVIDENCE_NO_SIM'


def test_15pct_budget_is_enforced_even_with_positive_r_and_money():
    s,ci,c,r=inputs();s['REFERENCE_2T']['S0']['maximum_m1_close_drawdown_pct']=.151
    assert judgment(s,ci,c,r,assessment(),82)['status']=='INSUFFICIENT_EVIDENCE_NO_SIM'


def test_verified_clear_negative_interval_ends_sim_promotion():
    s,ci,c,r=inputs()
    s['REFERENCE_2T']['S0'].update(mean_net_r=-.1,total_marked_pnl=-10000)
    for b in ('5','10'):ci['REFERENCE_2T']['blocks'][b]['S0']['mean_net_r_95ci']=[-.2,-.01]
    assert judgment(s,ci,c,r,assessment(),82)['status']=='END_CURRENT_VERSION_SIM_PROMOTION'


def test_conflicting_positive_money_and_negative_r_remains_insufficient_even_with_negative_interval():
    s,ci,c,r=inputs();s['REFERENCE_2T']['S0']['mean_net_r']=-.1
    for b in ('5','10'):ci['REFERENCE_2T']['blocks'][b]['S0']['mean_net_r_95ci']=[-.2,-.01]
    assert judgment(s,ci,c,r,assessment(),82)['status']=='INSUFFICIENT_EVIDENCE_NO_SIM'


def test_all_criteria_pass_still_does_not_enable_orders():
    result=judgment(*inputs(),assessment(),82)
    assert result['status']=='ECONOMIC_CRITERIA_PASS_EXECUTION_REVIEW_STILL_REQUIRED'
    assert result['simulation_orders_enabled'] is False


def test_synchronous_blocks_keep_zero_trade_days_and_identical_arm_difference_zero():
    days=list(range(20));a=dict(r=np.r_[np.zeros(10),np.ones(10)*.1],n=np.r_[np.zeros(10),np.ones(10)],pnl=np.ones(20))
    result=block_intervals(dict(S0=a,ORDINARY=a),days,replicates=100)
    assert result['calendar_days_including_zero_trades']==20
    assert result['blocks']['5']['first_minus_second']['mean_net_r_95ci']==[0.,0.]


def test_zero_trade_bootstrap_does_not_invent_positive_evidence():
    a=dict(r=np.zeros(20),n=np.zeros(20),pnl=np.zeros(20))
    result=block_intervals(dict(S0=a,ORDINARY=a),list(range(20)),replicates=100)
    assert result['blocks']['10']['S0']['mean_net_r_95ci'] is None


def test_registered_82_days_do_not_substitute_for_60_observed_complete_days():
    result=judgment(*inputs(),assessment(),59)
    assert result["status"]=="INSUFFICIENT_EVIDENCE_CAPACITY"
