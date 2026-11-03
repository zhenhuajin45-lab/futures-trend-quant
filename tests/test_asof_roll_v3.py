from datetime import timedelta

import pandas as pd
import pytest

from backtest.asof_roll import CN, build_asof_minutes, validity_windows
from backtest.exit_experiment_strategy import ExitExperimentFactsStrategy
from backtest.facts_runner import FactsOnlyRunner
from backtest.runner import ReplayBar
from backtest.smoke import synthetic_events
from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.data.gm_bars import bar_from_mapping


CS, OLD, NEW = "SHFE.AU", "SHFE.au2608", "SHFE.au2610"


def _time(s):
    return pd.Timestamp(s, tz=CN)


def _quote(sym, start, price, day=None, lag=0):
    end = start + timedelta(minutes=1)
    return dict(symbol=sym, bob=start, eob=end, trading_date=day or start.date(),
                open=price, high=price+1, low=price-1, close=price, available_at=end+timedelta(minutes=lag))


@pytest.mark.parametrize("gap",[-3.,10.])
@pytest.mark.parametrize("has_night",[True,False])
def test_use_last_known_prior_close_or_wait_for_first_joint_tick(gap,has_night):
    prev = _time("2026-01-05 14:59")
    day_open = _time("2026-01-06 09:00")
    start = _time("2026-01-05 21:00") if has_night else day_open
    day = day_open.date()
    rows = [_quote(OLD,prev,80),_quote(NEW,prev,80+gap)]
    rows += [_quote(NEW,start,80+gap,day),_quote(OLD,start,80,day),
             _quote(NEW,start+timedelta(minutes=1),82+gap,day)]
    mapping = pd.DataFrame([[prev.date(),OLD,prev], [day,NEW,start]],columns=["trading_date","symbol","known_at"])
    joined, audit = build_asof_minutes(CS,mapping,pd.DataFrame(rows))
    assert joined.close.tolist()==[80,80,82]
    assert audit[0]["mode"]=="PREVIOUS_SESSION_CLOSE"
    assert audit[0]["delta"]==gap
    assert audit[0]["cross_night"] is (has_night or prev.date()!=start.date())
    missing = pd.DataFrame([rows[0],*rows[2:]])
    joined, audit = build_asof_minutes(CS,mapping,missing)
    assert joined.eligible.tolist()==[True,False,True]
    assert joined.reason.tolist()[1]=="WAIT_SYNCHRONOUS_PAIR"
    assert audit[0]["new_eob"]==audit[0]["old_eob"]==start+timedelta(minutes=1)
    assert audit[0]["effective_bob"]>=max(audit[0]["available"],audit[0]["mapping_known_at"])
    # Two-leg bridge must be RECEIVED. A late old leg cannot be backfilled.
    missing.loc[missing.symbol.eq(OLD)&missing.bob.eq(start),"available_at"] = start+timedelta(hours=1)
    joined, audit = build_asof_minutes(CS,mapping,missing)
    assert joined.eligible.tolist()==[True,False,False]
    assert not audit
    joined, _ = build_asof_minutes(CS,mapping.assign(known_at=pd.NaT),missing)
    assert not joined.eligible.any() and joined.close.isna().all()


def test_later_close_revision_and_append_preserve_inputs_facts_states_and_entry_prefix():
    # Long enough to observe actual shared-core decisions and entry evidence after H1 warmup.
    rows = []
    events = synthetic_events(4500)
    for item in events:
        bar = item.bar
        start = pd.Timestamp(bar.bob,tz=CN)
        for sym,gap in ((OLD,0.),(NEW,12.)):
            row = _quote(sym,start,bar.close+gap)
            row.update(open=bar.open+gap,high=bar.high+gap,low=bar.low+gap)
            rows.append(row)
    quotes = pd.DataFrame(rows)
    dates = sorted(set(quotes.trading_date))
    mapping = pd.DataFrame([dict(trading_date=d, symbol=OLD if i<len(dates)-1 else NEW,
                                 known_at=_time(str(d))-timedelta(days=1))
                            for i,d in enumerate(dates)])
    cutoff = quotes.eob.max()-timedelta(minutes=90)
    revised = quotes.copy()
    for k in ("open","high","low","close"):
        revised.loc[revised.bob>=cutoff,k] += 20
    late_map = pd.concat([mapping,pd.DataFrame([dict(trading_date=dates[-1],symbol=OLD,
                                                     known_at=cutoff+timedelta(minutes=1))])])
    original,_ = build_asof_minutes(CS,mapping,quotes)
    mutated,_ = build_asof_minutes(CS,late_map,revised)
    appended,_ = build_asof_minutes(CS,late_map[late_map.known_at<=cutoff],
                                    revised[revised.available_at<=cutoff])
    left = original[original.eob<=cutoff].reset_index(drop=True)
    right = mutated[mutated.eob<=cutoff].reset_index(drop=True)
    pd.testing.assert_frame_equal(left,right)
    pd.testing.assert_frame_equal(left,appended.reset_index(drop=True))
    def replay_inputs(frame):
        assert frame.eligible.all()
        replay = FactsOnlyRunner(strategy_step=ExitExperimentFactsStrategy(
            "S0",load_config(default_config_path()).raw["rules"],{CS:CS}
        )).run(ReplayBar(bar_from_mapping(row),row["trading_date"]) for row in frame.to_dict("records"))
        return [dict(rec.values) for rec in replay.facts if rec.eob<=cutoff and "trend_decision" in rec.values]
    baseline = replay_inputs(original)
    assert len(baseline)>30 and any(row["entry_evaluation"] for row in baseline)
    assert replay_inputs(mutated)==replay_inputs(appended)==baseline


def test_validity_checks_label_horizon_pivot_and_preceding_warmup():
    mask=[True]*1500
    mask[1250]=False
    assert validity_windows(mask,1260,1290)=={
        "confirmation":True,"future_48_label":True,"pivot_to_confirmation":True,
        "pivot_atr_history":False,"strategy_warmup":False,
    }
    assert not validity_windows(mask,1200,1220)["future_48_label"]
    assert not validity_windows(mask,1200,1300)["pivot_to_confirmation"]
    assert not validity_windows(mask,1200,1490)["future_48_label"]


def test_single_late_quote_cannot_insert_a_bar_before_it_arrived():
    first = _time("2026-01-06 09:00")
    rows = [_quote(OLD, first, 100),
            _quote(OLD, first+timedelta(minutes=1), 102, lag=30),
            _quote(OLD, first+timedelta(minutes=2), 104)]
    mapping = pd.DataFrame([dict(trading_date=first.date(), symbol=OLD, known_at=first)])
    full, _ = build_asof_minutes(CS, mapping, pd.DataFrame(rows))
    cutoff = first+timedelta(minutes=10)
    prefix, _ = build_asof_minutes(CS, mapping, pd.DataFrame(rows).query("available_at <= @cutoff"))
    pd.testing.assert_frame_equal(full,prefix)
    assert full.close.tolist() == [100,104]


def test_recorded_cutoff_accepts_latency_but_does_not_advance_mapping_or_roll():
    start = _time("2026-01-06 09:00")
    rows = []
    for i in range(6):
        for symbol, gap in ((OLD, 0), (NEW, 10)):
            row = _quote(symbol, start+timedelta(minutes=i), 100+i+gap)
            end = row["eob"]
            row["available_at"] = end + pd.Timedelta(milliseconds=100)
            row["decision_at"] = end + pd.Timedelta(milliseconds=250)
            rows.append(row)
    # New mapping arrives after the second bar has started. First synchronous
    # pair thereafter is received 100 ms AFTER bar 3's start. Bar 4 can use it.
    published = start + pd.Timedelta(minutes=1, milliseconds=50)
    mapping = pd.DataFrame([dict(symbol=OLD, trading_date=start.date(), known_at=start),
                            dict(symbol=NEW, trading_date=start.date(), known_at=published)])
    q = pd.DataFrame(rows)
    q.loc[(q.symbol == NEW) & (q.bob == start+timedelta(minutes=4)), "available_at"] += pd.Timedelta(seconds=1)
    joined, audit = build_asof_minutes(CS, mapping, q)
    assert joined.actual_contract.tolist()[:2] == [OLD,OLD]
    assert joined.reason.tolist() == ["OK","OK","WAIT_SYNCHRONOUS_PAIR","OK","ACTIVE_QUOTE_MISSING_OR_LATE","OK"]
    assert audit[0]["effective_bob"] >= max(audit[0]["available"], audit[0]["mapping_known_at"])
    assert joined.iloc[3].close == 103
    assert (joined.decision_at - joined.eob == pd.Timedelta(milliseconds=250)).all()
    for cutoff in (joined.iloc[3].decision_at, joined.iloc[3].eob+pd.Timedelta(milliseconds=150)):
        received = q[(q.available_at <= cutoff) & (q.decision_at <= cutoff)]
        prefix, _ = build_asof_minutes(CS, mapping[mapping.known_at <= cutoff], received)
        pd.testing.assert_frame_equal(joined[joined.decision_at <= cutoff], prefix)
    nominal, _ = build_asof_minutes(CS, mapping, q.drop(columns="decision_at"))
    assert nominal.empty


def test_explicit_clock_must_be_complete_consistent_and_within_one_minute():
    start = _time("2026-01-06 09:00")
    q = pd.DataFrame([_quote(OLD,start,100), _quote(NEW,start,120)])
    mapping = pd.DataFrame([dict(symbol=OLD,trading_date=start.date(),known_at=start)])
    for clock in (pd.NaT, start, start+timedelta(minutes=2), start+pd.Timedelta(minutes=1, milliseconds=1)):
        bad = q.assign(decision_at=q.eob)
        bad.loc[1,"decision_at"] = clock
        with pytest.raises(ValueError, match="decision_at"):
            build_asof_minutes(CS, mapping, bad)
