"""Account identities and immutable experiment scope for the final comparison."""
import pandas as pd
import pytest
from tools.same_discipline_v10.report import ledger_check
from tools.short_swing_v9.independent import ROOT
from tools.historical_strategy.reproduce import canonical_digest
from backtest.forward_v3 import read


def test_closed_and_end_open_cash_identity():
    t=pd.DataFrame([dict(modeled_net_pnl=90.,gross_pnl=100.,modeled_fee=10.,initial_risk_money=100.,modeled_net_r=.9)])
    o=pd.DataFrame([dict(unrealized_net_of_paid_entry_fee=20.)])
    s=dict(ending_equity=1_000_110.,closed_trades=1)
    assert ledger_check(t,o,s)['cash_identity_error']==0
    s['ending_equity']+=10
    with pytest.raises(ValueError,match='identity'):ledger_check(t,o,s)


def test_final_experiment_keeps_account_policy_and_costs_unchanged():
    r=read(ROOT/'tools/same_discipline_v10/registration.json')
    assert r['D_only_account'].startswith('PROHIBITED')
    assert not r['orders'] and not r['new_data_requests'] and not r['independent_test_consumed']
    assert r['holding_policy']==dict(buffer_minutes=5,weekend_mode='DAY_CLOSE')
    assert all(canonical_digest(ROOT/p)==sha for p,sha in r['account_source_hashes'].items())
