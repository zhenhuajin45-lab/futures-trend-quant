import importlib.util
from pathlib import Path


def module(name):
    s=importlib.util.spec_from_file_location(name,Path(__file__).with_name(name+".py"))
    m=importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def test_raw_callback_observation_and_chain_corruption(tmp_path):
    recorder=module("recorder_v5")
    auditor=module("recorder_audit_v5")
    recorder.JOURNAL=recorder.Journal(tmp_path/"run")
    class Context:
        subscriptions={"SHFE.au2612"}
    bar=dict.fromkeys(recorder.RAW_BAR_FIELDS, 0)
    bar.update(symbol="SHFE.au2612",eob="2026-11-04T00:30:00+08:00",close=1,receive_local_time=1234)
    recorder.on_bar(Context(),[bar])
    recorder.on_market_data_disconnected(Context())
    recorder.JOURNAL.stream.close()
    path=tmp_path/"run"/"raw.jsonl"
    result=auditor.inspect_journal(path)
    assert result["chain_failure"] is None and result["events"]=={"BAR":1,"MARKET_DISCONNECTED":1}
    assert result["callbacks_by_contract"]=={"SHFE.au2612":1}
    assert "decision_at" not in path.read_text()
    data=path.read_bytes().replace(b'"close": 1',b'"close": 2')
    path.write_bytes(data)
    assert auditor.inspect_journal(path)["chain_failure"]["line"]==2


def test_real_sdk_barlike_wrapper_reaches_raw_journal(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone
    from gm.csdk.c_sdk import BarLikeDict2
    recorder=module("recorder_v5")
    end=datetime(2026,11,4,0,40,tzinfo=timezone(timedelta(hours=8)))
    original=dict(symbol="SHFE.au2612",frequency="60s",open=10.0,high=11.0,low=9.0,close=10.5,
                  volume=3,amount=100.0,position=5,pre_close=9.5,bob=end-timedelta(minutes=1),
                  eob=end,receive_local_time=1234.5)
    bar=BarLikeDict2(original)
    assert not callable(bar.keys)
    recorder.JOURNAL=recorder.Journal(tmp_path/"sdk_wrapper")
    class Context:
        subscriptions={"SHFE.au2612"}
    recorder.on_bar(Context(),[bar])
    recorder.JOURNAL.stream.close()
    record=json.loads((tmp_path/"sdk_wrapper"/"raw.jsonl").read_text(encoding="utf8"))
    expected={k:str(v) if isinstance(v,datetime) else v for k,v in original.items()}
    assert set(record["payload"])==set(recorder.RAW_BAR_FIELDS)
    assert record["payload"]==expected
    assert record["kind"]=="BAR"
    assert record["payload"]["receive_local_time"]==1234.5
    health=json.loads((tmp_path/"sdk_wrapper"/"health.json").read_text(encoding="utf8"))
    assert health["callbacks_by_contract"]=={"SHFE.au2612":1}


def test_windows_health_reader_lock_does_not_terminate_raw_capture(tmp_path,monkeypatch):
    recorder=module("recorder_v5")
    writer=recorder.Journal(tmp_path/"locked")
    original=Path.replace
    def locked(self,target):
        raise PermissionError("reader denies Windows rename")
    monkeypatch.setattr(Path,"replace",locked)
    writer.health(set())
    writer.append("STILL_CAPTURING",{})
    assert writer.seq==1 and writer.health_write_conflicts==1
    monkeypatch.setattr(Path,"replace",original)
    writer.health(set())
    assert (tmp_path/"locked"/"health.json").exists()
    writer.stream.close()


def test_dated_main_does_not_use_last_future_row_or_continuous_code():
    import pytest
    recorder=module("recorder_v5")
    rows=[dict(trade_date="2026-11-04",symbol="SHFE.au2612"),
          dict(trade_date="2026-11-05",symbol="SHFE.au2702")]
    assert recorder.main_for_day("SHFE.AU",rows,"2026-11-04")=="SHFE.au2612"
    assert recorder.main_for_day("SHFE.AU",list(reversed(rows)),"2026-11-05")=="SHFE.au2702"
    assert recorder.main_for_day("CZCE.CF",[dict(trade_date="2026-11-04",symbol="CZCE.CF701")],"2026-11-04")=="CZCE.CF701"
    for bad in ("SHFE.AU","SHFE.AU2612","SHFE.au99","DCE.au2612"):
        with pytest.raises(ValueError):
            recorder.main_for_day("SHFE.AU",[dict(trade_date="2026-11-04",symbol=bad)],"2026-11-04")
    with pytest.raises(ValueError):
        recorder.main_for_day("SHFE.AU",rows,"2026-11-06")


def test_calendar_selects_day_session_and_overnight_exchange_day():
    from datetime import datetime, timedelta, timezone
    import pytest
    recorder=module("recorder_v5")
    zone=timezone(timedelta(hours=8))
    class Context:
        calendar_days={
            "SHFE.AU":{datetime(2026,11,4,9,1,tzinfo=zone):"2026-11-04",
                       datetime(2026,11,4,15,tzinfo=zone):"2026-11-04",
                       datetime(2026,11,4,21,1,tzinfo=zone):"2026-11-05",
                       datetime(2026,11,5,0,1,tzinfo=zone):"2026-11-05"}}
    context=Context()
    assert recorder.subscription_day(context,"SHFE.AU",datetime(2026,11,4,8,tzinfo=zone))=="2026-11-04"
    assert recorder.subscription_day(context,"SHFE.AU",datetime(2026,11,4,21,tzinfo=zone))=="2026-11-05"
    assert recorder.subscription_day(context,"SHFE.AU",datetime(2026,11,4,16,tzinfo=zone))=="2026-11-05"
    assert recorder.subscription_day(context,"SHFE.AU",datetime(2026,11,5,0,tzinfo=zone))=="2026-11-05"
    with pytest.raises(ValueError):
        recorder.subscription_day(context,"DCE.M",datetime(2026,11,5,0,tzinfo=zone))


def test_refresh_subscribes_real_dated_main_and_adjacent_bridge(tmp_path,monkeypatch):
    from datetime import datetime, timedelta, timezone
    import json
    import gm.api
    recorder=module("recorder_v5")
    zone=timezone(timedelta(hours=8))
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return cls(2026,11,4,8,tzinfo=zone)
    monkeypatch.setattr(recorder,"datetime",Clock)
    rows=[dict(trade_date="2026-10-01",symbol="SHFE.au2610"),
          dict(trade_date="2026-11-03",symbol="SHFE.au2612"),
          dict(trade_date="2026-11-04",symbol="SHFE.au2612"),
          dict(trade_date="2026-11-05",symbol="SHFE.au2702")]
    monkeypatch.setattr(gm.api,"fut_get_continuous_contracts",lambda *args:rows)
    called=[]
    monkeypatch.setattr(gm.api,"subscribe",lambda **kwargs:called.append(kwargs["symbols"]))
    class Context:
        universe=["SHFE.AU"]
        subscriptions=set()
        calendar_days={"SHFE.AU":{datetime(2026,11,4,9,1,tzinfo=zone):"2026-11-04"}}
    recorder.JOURNAL=recorder.Journal(tmp_path/"refresh")
    recorder.refresh(Context())
    recorder.JOURNAL.stream.close()
    events=[json.loads(line) for line in (tmp_path/"refresh"/"raw.jsonl").read_text(encoding="utf8").splitlines()]
    coverage=events[-1]["payload"]
    assert coverage["main_contracts"]==["SHFE.au2612"]
    assert coverage["target_trading_days"]=={"SHFE.AU":"2026-11-04"}
    assert called==["SHFE.au2612","SHFE.au2702"]
    assert "SHFE.AU" not in called and "SHFE.au2610" not in called
