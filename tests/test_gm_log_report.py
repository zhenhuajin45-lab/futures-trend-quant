from pathlib import Path

from backtest.gm_log_report import parse_log


def test_gm_log_report_extracts_facts_and_counts_without_free_text(tmp_path: Path) -> None:
    source = tmp_path / "gm.log"
    source.write_text(
        "noise\n"
        '[M15_FACTS] {"symbol":"DCE.m2701","eob":"2026-10-20T09:15:00","close":1}\n'
        '[SIGNAL] {"accepted":true,"path":"D","reason":"PATH_D_ACCEPTED"}\n'
        '[ENTRY_GUARD] {"accepted":false,"reason":"PORTFOLIO_POSITION_CAP"}\n'
        '[EXIT_GUARD] {"reason":"TREND_EXIT"}\n',
        encoding="utf-8",
    )
    summary, facts = parse_log(source)
    assert summary["tag_counts"]["M15_FACTS"] == 1
    assert summary["accepted_paths"] == {"D": 1}
    assert summary["exit_reasons"] == {"TREND_EXIT": 1}
    assert facts[0]["symbol"] == "DCE.m2701"
