from backtest.parity import compare_m15_facts


def test_fact_parity_uses_numeric_tolerance_and_reports_exact_key() -> None:
    base = {
        "symbol": "SHFE.au2606",
        "eob": "2026-10-20T09:15:00",
        "close": 849.0,
        "ema34": 848.0,
        "atr14": 3.0,
        "slope_state": "up",
        "h1_absolute": "bull",
        "top_div": False,
        "bottom_div": False,
        "trend_decision": {"confirmed_state": "bull"},
    }
    almost = dict(base, ema34=848.0000005, slope_state="flat")
    mismatches = compare_m15_facts([base], [almost])
    assert [(item.key, item.reason) for item in mismatches] == [("slope_state", "value")]


def test_fact_parity_reports_missing_rows() -> None:
    row = {"symbol": "DCE.m2605", "eob": "2026-10-20T09:15:00"}
    mismatches = compare_m15_facts([row], [])
    assert len(mismatches) == 1
    assert mismatches[0].reason == "missing_row"
