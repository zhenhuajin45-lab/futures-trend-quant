import pytest
from tools.first_entry_v71.clock import make_lock,AdmissionError


def test_calendar_cannot_extend_declared_label_clock_past_may6():
    bundle=dict(coverage_start='2025-06-01',coverage_end='2026-05-07',products={'CZCE.CJ':{}},
        purpose='ENGINEERING_FIXTURE_ONLY',independent_of_quote_minutes=True,
        evidence_files=[dict(path='fixture.json',sha256='fixture')],source_policy={})
    registration=dict(new_sample=dict(universe=['CZCE.CJ'],proposed_prehistory_start='2025-06-01',
        proposed_scoring_trade_dates=['2026-01-05','2026-04-24'],proposed_price_path_end='2026-05-06T15:00:00+08:00'))
    with pytest.raises(AdmissionError,match='CALENDAR_RANGE_INSUFFICIENT'):
        make_lock(bundle,registration,'registration','calendar')
