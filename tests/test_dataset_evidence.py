from datetime import datetime, timedelta

import pandas as pd

from backtest.dataset_evidence import continuous_quality, dataset_id
from backtest.parity_coverage import build_parity_coverage


def test_dataset_id_changes_when_any_versioned_input_changes() -> None:
    base = dataset_id("manifest", "config", "facts", "code")
    assert base != dataset_id("manifest2", "config", "facts", "code")
    assert base != dataset_id("manifest", "config2", "facts", "code")
    assert base != dataset_id("manifest", "config", "facts2", "code")
    assert base != dataset_id("manifest", "config", "facts", "code2")


def test_continuous_quality_reports_duplicates_invalid_ohlc_and_non_asserted_missing() -> None:
    start = datetime(2026, 1, 5, 9, 1)
    frame = pd.DataFrame([
        {"symbol": "TEST.X", "eob": start, "open": 100, "high": 101, "low": 99, "close": 100, "trading_date": "2026-01-05"},
        {"symbol": "TEST.X", "eob": start, "open": 100, "high": 99, "low": 98, "close": 100, "trading_date": "2026-01-05"},
        {"symbol": "TEST.X", "eob": start + timedelta(minutes=3), "open": 101, "high": 102, "low": 100, "close": 101, "trading_date": "2026-01-05"},
    ])
    quality = continuous_quality(frame)
    assert quality["duplicate_symbol_eob_rows"] == 1
    assert quality["invalid_ohlc_rows"] == 1
    assert quality["observed_clock_gaps_gt_60s"] == 1
    assert quality["missing_minute_bars"] is None


def test_parity_coverage_does_not_overclaim_order_or_path_comparison() -> None:
    gm_rows = [{"symbol": "TEST.X1", "eob": "2026-01-05T09:15:00", "close": 100}]
    report = build_parity_coverage(
        gm_rows,
        {"gm_rows": 1, "offline_rows": 1, "mismatch_count": 0, "acceptance_passed": True},
        code_sha="abc",
    )
    coverage = {item["area"]: item["status"] for item in report["coverage"]}
    assert coverage["row_identity_and_time_boundary"] == "COMPARED"
    assert coverage["path_candidates_ABCD"] == "NOT_COMPARED"
    assert coverage["order_intent_and_sizing"] == "NOT_COMPARED"
