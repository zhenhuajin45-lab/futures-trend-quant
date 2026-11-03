from datetime import date, datetime, timedelta

import pytest

from backtest.continuous import ContractBar, RollEvent, apply_additive_back_adjustment
from futures_trend_quant.core.models import Bar
from futures_trend_quant.data.gm_export import ExportDataError, calculate_roll_differences


def _bar(symbol: str, day: int, close: float) -> ContractBar:
    bob = datetime(2026, 1, day, 9)
    return ContractBar(
        trading_date=date(2026, 1, day),
        contract=symbol,
        bar=Bar(
            symbol=symbol,
            frequency="60s",
            bob=bob,
            eob=bob + timedelta(minutes=1),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1,
            amount=1,
            position=1,
            pre_close=close,
        ),
    )


def test_additive_adjustment_removes_roll_gap() -> None:
    mapping = {date(2026, 1, 1): "DCE.m2605", date(2026, 1, 2): "DCE.m2609"}
    rows = [_bar("DCE.m2605", 1, 3000), _bar("DCE.m2609", 2, 3100)]
    rolls = [RollEvent(date(2026, 1, 2), "DCE.m2605", "DCE.m2609", 100)]
    adjusted = apply_additive_back_adjustment(rows, mapping, rolls)
    assert [bar.close for bar in adjusted] == [3000, 3000]


def test_roll_difference_requires_both_contracts_on_roll_date() -> None:
    mapping = {date(2026, 1, 1): "DCE.m2605", date(2026, 1, 2): "DCE.m2609"}
    with pytest.raises(ExportDataError, match="lacks same-day close"):
        calculate_roll_differences(mapping, {(date(2026, 1, 2), "DCE.m2609"): 3100})
