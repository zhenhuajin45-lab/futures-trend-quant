from datetime import datetime
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location("history_v5",Path(__file__).with_name("history_recheck_v5.py"))
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_session_and_bridge_window_preserve_weekend_and_holiday():
    first=datetime.fromisoformat("2026-08-21T21:00:00")
    bridge=datetime.fromisoformat("2026-08-21T14:59:00")
    last=datetime.fromisoformat("2026-08-24T15:00:00")
    assert module.request_window(first,last,bridge)==(bridge,last)
    holiday_end=datetime.fromisoformat("2026-10-09T15:00:00")
    assert module.request_window(first,holiday_end,bridge)[0]==bridge
    listed=datetime.fromisoformat("2026-08-21T21:00:00")
    assert module.request_window(first,last,bridge,listed)[0]==listed
    with pytest.raises(ValueError):
        module.request_window(first,last,listed_at=datetime.fromisoformat("2026-08-25"))


def test_recovered_minutes_have_no_claim_to_historical_arrival():
    import pandas as pd
    t=module.stamp("2026-08-21T21:01:00+08:00")
    rows=[dict(symbol="DCE.eg2610",eob=t,open=1,high=1,low=1,close=1),
          dict(symbol="DCE.eg2610",eob=t+pd.Timedelta(minutes=1),open=1,high=1,low=1,close=1)]
    result=module.compare_frames(pd.DataFrame(rows[1:]),pd.DataFrame(rows),t,t+pd.Timedelta(minutes=1))
    assert result["newly_recovered_m1"]==1 and result["common_ohlc_changed"]==0
    assert "available_at" not in result
