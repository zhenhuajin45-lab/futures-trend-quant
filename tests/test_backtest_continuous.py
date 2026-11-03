from datetime import date, datetime, timedelta

import pytest

from backtest.continuous import ContractBar, RollEvent, apply_additive_back_adjustment
from futures_trend_quant.core.models import Bar
from futures_trend_quant.data.gm_export import (
    ExportDataError,
    calculate_roll_differences,
    export_history_to_parquet,
)


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


def test_continuous_adjustment_can_emit_stable_csymbol_identity() -> None:
    mapping = {date(2026, 1, 1): "DCE.m2605", date(2026, 1, 2): "DCE.m2609"}
    rows = [_bar("DCE.m2605", 1, 3000), _bar("DCE.m2609", 2, 3100)]
    rolls = [RollEvent(date(2026, 1, 2), "DCE.m2605", "DCE.m2609", 100)]
    adjusted = apply_additive_back_adjustment(
        rows, mapping, rolls, continuous_symbol="DCE.M"
    )
    assert {bar.symbol for bar in adjusted} == {"DCE.M"}


def test_roll_difference_requires_both_contracts_on_roll_date() -> None:
    mapping = {date(2026, 1, 1): "DCE.m2605", date(2026, 1, 2): "DCE.m2609"}
    with pytest.raises(ExportDataError, match="lacks same-day close"):
        calculate_roll_differences(mapping, {(date(2026, 1, 2), "DCE.m2609"): 3100})


def test_history_export_materializes_requested_frequency(tmp_path) -> None:
    import pandas as pd

    def fetch(**_):
        return pd.DataFrame(
            [{
                "symbol": "SHFE.au2612",
                "bob": datetime(2026, 10, 20, 9, 0),
                "eob": datetime(2026, 10, 20, 9, 1),
                "open": 850.0,
                "high": 850.2,
                "low": 849.8,
                "close": 850.1,
                "volume": 10,
            }]
        )

    output = tmp_path / "bars.parquet"
    export_history_to_parquet(
        symbol="SHFE.au2612",
        start_time=datetime(2026, 10, 20, 9, 0),
        end_time=datetime(2026, 10, 20, 9, 1),
        output_path=output,
        history_fetcher=fetch,
    )
    frame = pd.read_parquet(output)
    assert frame.loc[0, "frequency"] == "60s"


def test_history_export_overlaps_one_bar_at_chunk_boundaries(tmp_path) -> None:
    import pandas as pd

    starts = []

    def fetch(**kwargs):
        starts.append(kwargs["start_time"])
        return pd.DataFrame(
            [{
                "symbol": "SHFE.au2612",
                "bob": datetime(2026, 1, 1, 9, 0),
                "eob": datetime(2026, 1, 1, 9, 1),
                "open": 850.0,
                "high": 850.2,
                "low": 849.8,
                "close": 850.1,
                "volume": 10,
            }]
        )

    export_history_to_parquet(
        symbol="SHFE.au2612",
        start_time=datetime(2026, 1, 1),
        end_time=datetime(2026, 1, 2, 0, 1),
        output_path=tmp_path / "overlap.parquet",
        frequency="60s",
        chunk_days=1,
        history_fetcher=fetch,
    )
    assert starts == [datetime(2026, 1, 1), datetime(2026, 1, 1, 23, 59)]
