from backtest.smoke import run_smoke


def test_shared_core_smoke_produces_complete_m15_facts() -> None:
    report = run_smoke()
    assert report["purpose"] == "ENGINEERING_SMOKE_ONLY"
    assert report["performance_interpretation_allowed"] is False
    assert report["initial_cash"] == 1_000_000
    assert report["m15_callbacks"] == 300
    assert report["complete_m15_facts"] > 0
    assert report["runtime_csymbols"] == ["SHFE.AU"]
