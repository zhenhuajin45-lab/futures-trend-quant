from datetime import date, datetime

from futures_trend_quant.data.development_export import (
    assign_trading_date,
    build_continuous_bars,
    contract_export_windows,
    validate_dataset_manifest,
)
from futures_trend_quant.core.models import Bar


def _bar(symbol: str, bob: datetime, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        frequency="60s",
        bob=bob,
        eob=bob.replace(minute=bob.minute + 1),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=1,
    )


def test_contract_windows_extend_old_leg_through_roll_date() -> None:
    mapping = {
        date(2026, 1, 5): "DCE.m2605",
        date(2026, 1, 6): "DCE.m2605",
        date(2026, 1, 7): "DCE.m2609",
    }
    windows = {item.symbol: item for item in contract_export_windows(mapping)}
    assert windows["DCE.m2605"].end_time == datetime(2026, 1, 7, 16)
    assert windows["DCE.m2609"].start_time == datetime(2026, 1, 6, 20)


def test_trading_date_uses_next_exchange_date_for_night_session_and_holiday() -> None:
    calendar = [date(2026, 1, 5), date(2026, 1, 8)]
    assert assign_trading_date(datetime(2026, 1, 4, 21), calendar) == date(2026, 1, 5)
    assert assign_trading_date(datetime(2026, 1, 7, 21), calendar) == date(2026, 1, 8)
    assert assign_trading_date(datetime(2026, 1, 8, 9), calendar) == date(2026, 1, 8)


def test_continuous_builder_uses_roll_day_old_and_new_closes() -> None:
    mapping = {date(2026, 1, 5): "DCE.m2605", date(2026, 1, 6): "DCE.m2609"}
    bars = [
        _bar("DCE.m2605", datetime(2026, 1, 5, 15), 3000),
        _bar("DCE.m2605", datetime(2026, 1, 6, 15), 3010),
        _bar("DCE.m2609", datetime(2026, 1, 6, 15), 3110),
    ]
    adjusted, rolls, trading_dates = build_continuous_bars(
        csymbol="DCE.M", mapping=mapping, contract_bars=bars
    )
    assert [item.price_difference for item in rolls] == [100]
    assert [bar.close for bar in adjusted] == [3000, 3010]
    assert {bar.symbol for bar in adjusted} == {"DCE.M"}
    assert trading_dates == [date(2026, 1, 5), date(2026, 1, 6)]


def test_manifest_validation_fails_closed_when_export_is_incomplete(tmp_path) -> None:
    try:
        validate_dataset_manifest(tmp_path)
    except Exception as exc:
        assert "manifest is missing" in str(exc)
    else:
        raise AssertionError("incomplete export must not be accepted")
