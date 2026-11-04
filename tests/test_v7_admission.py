import pytest
from tools.first_entry_v7.source_qualification import validate_window, require_qualified
from tools.first_entry_v7.symmetric import comparison


OLD = ('2026-05-06T21:00:00+08:00', '2026-11-02T15:00:00+08:00')
FORWARD = '2026-11-04T21:00:00+08:00'


def test_qualified_proposed_window_does_not_touch_old_or_forward():
    validate_window('2026-01-05T09:15:00+08:00', '2026-04-24T15:00:00+08:00',
                    '2026-05-06T15:00:00+08:00', *OLD, FORWARD)


@pytest.mark.parametrize('start,end,tail', [
    ('2026-05-06T21:00:00+08:00', '2026-05-07T15:00:00+08:00', '2026-05-12T15:00:00+08:00'),
    ('2026-01-05T09:15:00+08:00', '2026-04-24T15:00:00+08:00', '2026-05-07T15:00:00+08:00'),
    ('2026-11-03T09:15:00+08:00', '2026-11-03T15:00:00+08:00', '2026-11-05T15:00:00+08:00')])
def test_anchor_or_future_path_contamination_blocks(start, end, tail):
    with pytest.raises(ValueError):
        validate_window(start, end, tail, *OLD, FORWARD)


def test_unqualified_rejected_before_input_access():
    with pytest.raises(ValueError, match='READY_FOR_DATA'):
        require_qualified({'status': 'READY_FOR_DATA'}, {})


def test_paired_numerator_and_dual_touch_bounds():
    days = [f'2026-01-{d:02d}' for d in range(1, 16)]
    rows = [(days[0], 'TARGET_FIRST', 'STOP_FIRST'),
            (days[1], 'BOTH_SAME_M1', 'TARGET_FIRST'),
            (days[2], 'STOP_FIRST', 'TARGET_FIRST')]
    result = comparison(rows, days)
    assert result['target_difference']['numerator'] == -1
    assert result['stop_difference']['numerator'] == 0
    assert result['ambiguous_lower']['difference'] == -1/3
    assert result['ambiguous_upper']['difference'] == 0
    assert result == comparison(rows, days)


def test_cli_blocks_unqualified_before_replay_access(tmp_path):
    import json
    from tools.first_entry_v7.run_selector import run
    manifest = tmp_path/'manifest.json'
    manifest.write_text(json.dumps({'status': 'READY_FOR_DATA'}), encoding='utf8')
    with pytest.raises(ValueError, match='READY_FOR_DATA'):
        run(manifest, tmp_path/'does_not_exist', tmp_path/'output')
    assert not (tmp_path/'output').exists()


def test_warmup_counts_raw_closed_bars_not_only_emitted_replay_rows():
    import pandas as pd
    from tools.first_entry_v7.run_selector import complete_past_ends
    expected = list(pd.date_range('2026-01-05T09:01:00+08:00', periods=30, freq='min'))
    assert len(complete_past_ends(expected, expected)) == 2
    assert len(complete_past_ends(expected[:5]+expected[6:], expected)) == 1
    with pytest.raises(ValueError, match='duplicate'):
        complete_past_ends(expected+[expected[0]], expected)
