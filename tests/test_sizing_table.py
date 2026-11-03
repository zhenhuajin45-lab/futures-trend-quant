import csv
from pathlib import Path

import pytest

from backtest.fees import UnverifiedFeeConfigError, load_fee_schedules
from backtest.sizing_table import ProductSizingInput, build_sizing_matrix, lots_for_stop


ROOT = Path(__file__).resolve().parents[1]


def test_sizing_applies_risk_and_adv_caps() -> None:
    item = ProductSizingInput(
        product="M",
        multiplier=10,
        price_tick=1,
        median_price=3000,
        atr14=50,
        average_daily_volume=1200,
        estimated_round_trip_cost_per_lot=5,
    )
    row = lots_for_stop(item, capital=1_000_000, risk_pct=0.01, stop_atr=1.5)
    assert row["risk_lots"] == 13
    assert row["adv_cap_lots"] == 1
    assert row["final_lots"] == 1
    assert len(build_sizing_matrix([item])) == 9


def test_unresolved_fee_config_and_placeholder_report_fail_closed() -> None:
    with pytest.raises(UnverifiedFeeConfigError, match="unresolved"):
        load_fee_schedules(ROOT / "config" / "fees.json")
    with (ROOT / "reports" / "sizing_table.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 12
    assert {row["product"] for row in rows} == {
        "LH", "M", "CF", "LC", "SA", "JD", "V", "EG", "AU", "AG", "CJ", "SR"
    }
    assert {row["status"] for row in rows} == {"EVIDENCE_MISSING"}
    unknown_columns = {
        "data_start",
        "data_end",
        "median_price",
        "atr14_median",
        "average_daily_volume",
        "account_500k_lots_1_5atr",
        "account_500k_lots_3atr",
        "account_500k_lots_6atr",
        "account_1m_lots_1_5atr",
        "account_1m_lots_3atr",
        "account_1m_lots_6atr",
        "account_2m_lots_1_5atr",
        "account_2m_lots_3atr",
        "account_2m_lots_6atr",
    }
    assert all(row[column] == "" for row in rows for column in unknown_columns)
