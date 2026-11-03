"""Exercise the sealed score entry point on synthetic data, without the GM SDK."""
from datetime import datetime

import numpy as np
import pandas as pd

from backtest import asof_roll, forward_v3 as fw
from backtest.forward_scores import GROUPS


def test_fixed_window_asof_roll_replay_and_sealed_score(tmp_path, monkeypatch):
    # Real source readers, receipt validation, causal stitching, M1 replay and all scorers.
    # Only the external code freeze and wall clock are replaced by synthetic counterparts.
    product = "CZCE.CF"
    lock = {"freeze_time": "2026-11-03T20:00:00+08:00", "universe": [product]}
    monkeypatch.setattr(fw, "check_lock", lambda: lock)

    class AfterLastSession(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2028, 1, 1, tzinfo=tz)

    monkeypatch.setattr(fw, "datetime", AfterLastSession)
    study = tmp_path / "sealed"
    fw.prepare(study)
    warm_days = pd.bdate_range(end="2026-11-03", periods=80)
    score_days = pd.bdate_range(start="2026-11-04", periods=90)
    previous = (warm_days[0] - pd.offsets.BDay()).date().isoformat()
    calendar_hashes = {}
    n_minutes = 0
    for index, day in enumerate(warm_days.append(score_days)):
        trading = day.date().isoformat()
        path = study / "days" / trading
        path.mkdir()
        segments = ([("09:00", "10:15"), ("10:30", "11:30"), ("13:30", "15:00")]
                    if index < len(warm_days) else [("14:30", "15:00")])
        segments = [[f"{trading}T{t}:00+08:00" for t in interval] for interval in segments]
        session = dict(date=trading, previous=previous, products={product: segments}, source="synthetic")
        fw.write(path / fw.FILES[0], session)
        t = pd.DatetimeIndex(fw.expected_times(segments))
        price = 1000 + 20 * np.sin(np.arange(n_minutes, n_minutes+len(t))/82)
        n_minutes += len(t)
        # A new contract costs exactly +20. Its overlap on the previous day permits a timely roll.
        roll_index = len(warm_days) + 18
        actual = "CZCE.CF701" if index < roll_index else "CZCE.CF705"
        legs = [actual] if index != roll_index-1 else [actual, "CZCE.CF705"]
        quotes = []
        for leg in legs:
            close = price + 20 * (leg == "CZCE.CF705")
            quotes.append(pd.DataFrame(dict(csymbol=product, symbol=leg, trading_date=trading,
                bob=t-pd.Timedelta(minutes=1), eob=t, available_at=t+pd.Timedelta(milliseconds=50),
                decision_at=t+pd.Timedelta(milliseconds=100), open=close,
                close=close, high=close+.1, low=close-.1)))
        pd.concat(quotes).to_parquet(path / fw.FILES[1], index=False)
        pd.DataFrame([dict(csymbol=product, symbol=actual, trading_date=trading,
                           known_at=segments[0][0])]).to_parquet(path / fw.FILES[2], index=False)
        hashes = {f: fw.digest(path / f) for f in fw.FILES}
        fw.write(path / "receipt.json", {**fw.calendar_record(session, lock), "hashes": hashes})
        calendar_hashes[trading] = hashes["sessions.json"]
        previous = trading
    manifest = tmp_path / "calendar.json"
    fw.write(manifest, dict(authority="synthetic", version="1", session_sha256_by_day=calendar_hashes))
    roll_events = []
    stitch = asof_roll.build_asof_minutes

    def observe_roll(*args):
        frame, switches = stitch(*args)
        roll_events.extend(switches)
        return frame, switches

    monkeypatch.setattr(asof_roll, "build_asof_minutes", observe_roll)
    result = fw.evaluate(study, manifest)
    assert len(roll_events) == 1 and roll_events[0]["cumulative_offset"] == -20
    assert result["first_day"] == score_days[0].date().isoformat()
    assert result["last_day"] == score_days[-1].date().isoformat()
    assert 0 < result["complete"] < 300 and result["local_status"] == "INCONCLUSIVE"
    assert result["censored"] > 0
    groups = fw.read(study / "unblinded" / "groups.json")
    assert set(groups) == set(GROUPS)
    events = fw.read(study / "unblinded" / "events.json")
    assert len(events) == result["complete"] + result["censored"]
    assert all(set(e["groups"]) == set(GROUPS) for e in events)
    assert result["per_product"][product]["groups"]["S0"]["entry_funnel"]["complete_bars"] == 180
    assert result["per_product"][product]["labels"]["unscored"] == 48
    assert fw.evaluate(study, manifest) == result
    assert len(roll_events) == 1  # re-opened the sealed results without re-running the model
