from datetime import datetime, timedelta, timezone

from backtest.trend_report import build_trend_report
from futures_trend_quant.core.models import Bar


def _bar(index: int, symbol: str = "TEST.X1") -> Bar:
    start = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    bob = start + timedelta(minutes=15 * index)
    close = 100.0 + index
    return Bar(
        symbol=symbol,
        frequency="900s",
        bob=bob,
        eob=bob + timedelta(minutes=15),
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1,
    )


def test_trend_report_extracts_confirmed_state_and_keeps_future_labels_post_run() -> None:
    bars = [_bar(index) for index in range(60)]
    facts = [
        {
            "symbol": bar.symbol,
            "csymbol": "TEST.X",
            "eob": bar.eob.isoformat(),
            "atr14": 1.0,
            "trend_decision": {"state": {"trend_state": "bull"}},
        }
        for bar in bars
    ]
    report = build_trend_report(facts, bars, dataset_label="unit-test")
    assert report["fact_rows"] == 60
    assert report["products"] == ["TEST.X"]
    assert report["performance_interpretation_allowed"] is False
    assert report["preregistered_sample_interpretation_allowed"] is False
    assert report["report_status"] == "ENGINEERING_DRY_RUN"
    assert report["pool"]["evaluated_bars"] == 12


def test_trend_report_fails_if_fact_bar_cannot_be_reconciled() -> None:
    bar = _bar(0)
    facts = [{
        "symbol": bar.symbol,
        "csymbol": "TEST.X",
        "eob": (bar.eob + timedelta(minutes=15)).isoformat(),
        "atr14": 1.0,
        "trend_decision": {"state": {"trend_state": "bull"}},
    }]
    try:
        build_trend_report(facts, [bar], dataset_label="unit-test")
    except ValueError as exc:
        assert "no matching M15 bar" in str(exc)
    else:
        raise AssertionError("missing fact/bar reconciliation must fail")
