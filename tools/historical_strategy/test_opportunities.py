from copy import deepcopy
import pandas as pd
from tools.historical_strategy.analyze import extract,match_controls
from tools.historical_strategy.inventory import contract_quality
from tools.historical_strategy.reproduce import canonical_digest,check_sources
import pytest


def test_matching_uses_no_future_outcomes_and_no_replacement():
    time=pd.Timestamp("2026-08-14T09:15:00+08:00")
    base=dict(actual_contract="DCE.eg2609",direction="bull",trading_date="2026-08-14",
              session="day_am_1",gate=True,atr=10.,close=100.,age=2,time=time)
    signals=[dict(base,opportunity_id="one"),dict(base,opportunity_id="two",time=time+pd.Timedelta(minutes=1))]
    controls=[dict(base,time=time-pd.Timedelta(minutes=15)),dict(base,time=time+pd.Timedelta(minutes=15))]
    first=match_controls(signals,controls)
    assert first["one"]["time"]==controls[0]["time"]
    assert len({c["time"] for c in first.values()})==2
    changed=deepcopy(controls)
    changed[0]["future_target_label"]="STOP_FIRST"
    changed[1]["future_target_label"]="TARGET_FIRST"
    second=match_controls(signals,changed)
    assert {k:v["time"] for k,v in first.items()}=={k:v["time"] for k,v in second.items()}


def test_invalid_stop_does_not_consume_later_first_valid_evidence():
    rows=[]
    for minute,anchor in ((15,0.),(30,98.),(45,98.)):
        row=dict(selected=True,decision_eob=f"2026-08-14T09:{minute:02d}:00+08:00",trading_date="2026-08-14",
                 bar=dict(bob="2026-08-14T09:00:00+08:00"),groups={"S0":dict(atr14=10.,close=100.,
                 trend_decision={"state":{"trend_state":"bull"}},entry_evaluation=dict(gate_open=False,
                 formed_paths=["B"],accepted=True,path="B",evidence_id="same",trigger_bar_id="trigger",anchor={"anchor_price":anchor}))})
        rows.append(row)
    rules=dict(stop_buffer_atr=.2,stop_min_atr=1.5,stop_max_atr=6.)
    ops,controls,flow,rejected=extract(dict(csymbol="DCE.EG",symbol="DCE.eg2609"),rows,1.,rules)
    assert len(ops)==1 and len(rejected)==1
    assert ops[0]["time"].minute==30 and ops[0]["repeat_rows"]==1
    assert flow["risk_rejected_signal_rows"]==1


def test_incomplete_buckets_do_not_count_toward_1200_warmup():
    clock=list(pd.date_range("2026-08-01T00:01:00+08:00",periods=1201*15,freq="min"))
    raw=pd.DataFrame(dict(eob=clock[1:]))
    quality,count=contract_quality(raw,clock)
    assert count==1200
    assert not quality[clock[-16]]
    assert quality[clock[-1]]


def test_reproduction_refuses_changed_rules_but_preserves_line_ending_equivalence(tmp_path):
    source=tmp_path/"rule.py"
    source.write_bytes(b"alpha\n")
    lock=dict(canonical_source_sha256={"rule.py":canonical_digest(source)})
    source.write_bytes(b"alpha\r\n")
    check_sources(lock,tmp_path)
    source.write_bytes(b"beta\n")
    with pytest.raises(ValueError,match="register a new study"):
        check_sources(lock,tmp_path)
