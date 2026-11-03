from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from backtest import forward_v3 as fw
from backtest.forward_scores import decide, moving_block_interval, space, summarize_product, GROUPS
from futures_trend_quant.core.models import Bar


def test_shared_day_moving_blocks_and_one_sided_prices():
    counts = [[2, 4]]*90
    assert moving_block_interval(counts, 5)["interval"] == [.5, .5]
    assert moving_block_interval(counts, 10)["replicates"] == 10000
    assert moving_block_interval([[0, 0]]*90, 5) is None
    assert space(100, 104, 101, 2, "bull") == {"forward_atr": 2,"adverse_atr":0, "range_atr":1.5}
    assert space(100, 99, 98, 2, "bear")["adverse_atr"] == 0


def test_prepare_ingest_idempotence_conflict_gap_and_early_score(tmp_path, monkeypatch):
    lock = {"freeze_time":"2026-11-03T21:30:00+08:00","universe":["CZCE.CF"]}
    monkeypatch.setattr(fw, "check_lock", lambda: lock)
    study, bundle = tmp_path/"study", tmp_path/"source"
    bundle.mkdir()
    fw.prepare(study)
    def make(day, prev):
        start, end = (day+"T09:"+v+":00+08:00" for v in ("00","01"))
        fw.write(bundle/"sessions.json",dict(date=day, previous=prev, source="synthetic",
                                           products={"CZCE.CF":[[start,end]]}))
        cols = dict(csymbol=["CZCE.CF"],trading_date=[day])
        pd.DataFrame(dict(**cols,symbol=["CZCE.CF701"],eob=[end],bob=[start],available_at=[end],
                          open=[10], high=[10], low=[10], close=[10])).to_parquet(bundle/"quotes.parquet")
        pd.DataFrame(dict(**cols,symbol=["CZCE.CF701"],known_at=[start])).to_parquet(bundle/"mapping.parquet")
    make("2026-10-29","2026-10-28")
    assert fw.ingest(study,bundle)["status"] == "INGESTED"
    assert fw.ingest(study,bundle)["status"] == "ALREADY_INGESTED"
    with (bundle/"sessions.json").open("a", encoding="utf-8") as h:
        h.write(" ")
    with pytest.raises(ValueError,match="conflicting"):
        fw.ingest(study,bundle)
    make("2026-11-02","2026-10-30")
    with pytest.raises(ValueError,match="non-consecutive"):
        fw.ingest(study,bundle)
    assert fw.evaluate(study)["score_days"] == 0
    assert not (study/"unblinded").exists()


def test_forward_output_tables_on_synthetic_prices():
    start = datetime(2027,1,4,9,tzinfo=timezone(timedelta(hours=8)))
    bars, rows = [], []
    for i in range(140):
        t = start + timedelta(minutes=15*i)
        c = 100+8*np.sin(i/4)
        b = Bar("CZCE.CF","900s",t,t+timedelta(minutes=15),c,c+.5,c-.5,c)
        bars.append(b)
        st = "bull" if i%18 < 9 else "bear"
        rows.append(dict(atr14=1, trend_decision={"state":{"trend_state":st}}, entry_evaluation=None))
    for i in (11,12):
        rows[i]["entry_evaluation"] = dict(accepted=True,path="D",evidence_id="D|bear|trigger",
                              gate_open=True,formed_paths=["D"],anchor_available=True)
    rep = dict(csymbol="CZCE.CF",bars=bars,trading_dates=[b.eob.date() for b in bars],
               groups={g:rows for g in GROUPS})
    p, events, ops = summarize_product(rep, {b.eob:"CT" for b in bars}, {("CT",b.eob):b for b in bars})
    assert ops[0]["membership"] == "shared" and ops[0]["groups"]["A"]["repeat_rows"] == 1
    assert ops[0]["groups"]["A"]["space8"]["range_atr"] > 0
    dates = [(date(2027,1,4)+timedelta(days=i)).isoformat() for i in range(90)]
    result = decide({"CZCE.CF":p},events,dates)
    assert result["complete"] > 0 and result["censored"] > 0
    assert result["delta"] == 0 and result["local_status"] == "INCONCLUSIVE"
    clock = {b.eob: b.eob + timedelta(seconds=1) for b in bars}
    _, _, delayed = summarize_product(rep, {b.eob:"CT" for b in bars},
                                     {("CT",b.eob):b for b in bars}, clock)
    observation = delayed[0]["groups"]["S0"]
    assert observation["clock_straddles_next_bar"]
    assert observation["from_eob_space8"] and observation["space8"] is None


@pytest.mark.parametrize(("improvement","status"), [(True,"LOCAL_REPLICATED"), (False,"NOT_REPLICATED")])
def test_local_ruling_is_independent_of_direction_hard_gate(improvement, status):
    dates = [(date(2027, 1, 4) + timedelta(days=i)).isoformat() for i in range(90)]
    product = {"labels":{"bull":10}, "groups":{}}
    for g in GROUPS:
        product["groups"][g] = {
            "bar_metrics":dict(direction_agreement=.5, directional_reference_bars=10, trend_segment_coverage=.7,
                               trend_segment_bars=10, range_switches_per_product_day=.5, range_product_days=2),
            "states":{"bars":20,"none_bars":1}, "agreement_by_direction_correct_total":{"bull":[5,10],"bear":[0,0]}}
    events = []
    for day in dates:
        for _ in range(4):
            group_rows = {}
            for g in GROUPS:
                win = improvement if g == "A" else not improvement
                group_rows[g] = dict(within_4_bars=win, delay_48=1 if win else 8, leg_bars=10,
                                     new_direction_bars=8 if win else 3, old_direction_bars=1 if win else 6,
                                     none_bars=1, confirmed_pair_at_zero=False,
                                     after_observed_pair={"eob":None, "space8":None})
            events.append(dict(csymbol="X",trading_date=day,label48_valid=True,groups=group_rows,
                               space8=None,already_moved_atr=3))
    outcome = decide({"X":product}, events, dates)
    assert outcome["local_status"] == status
    assert outcome["groups"]["A"]["hard_gates_pass"] is False
