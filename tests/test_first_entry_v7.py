from dataclasses import replace
from tools.first_entry_v7.selector import Evidence, Frame, filter_frames, frame_from_v6


def frame(n, key='a', direction='bull', **kwargs):
    return Frame('DCE.M', 'DCE.m2605', f'2026-01-05T09:{n*15:02d}:00+08:00', n, direction,
        (Evidence(key, key, True, True),) if key else (), previous_state_verified=True,
        previous_direction='none', **kwargs)


def reasons(frames):
    return [r['reason'] for r in filter_frames(frames)[0]]


def test_first_legal_only():
    assert reasons([frame(1), frame(2, 'b')]) == ['KEEP', 'EPISODE_SLOT_ALREADY_CONSUMED']


def test_duplicate_evidence():
    assert reasons([frame(1), frame(2)]) == ['KEEP', 'DUPLICATE_EVIDENCE']


def test_invalid_signal_then_valid():
    first = replace(frame(1), evidence=(Evidence('a', 'a', True, False),))
    assert reasons([first, frame(2, 'b')]) == ['NOT_VALID_AT_SIGNAL', 'KEEP']


def test_open_invalid_does_not_replace():
    # A future opening gap cannot enter the Frame schema or replenish its slot.
    decisions, _ = filter_frames([frame(1), frame(2, 'b')])
    future_labels = {'a': 'INVALID_AT_OPEN', 'b': 'TARGET_FIRST'}
    assert [d['opportunity_id'] for d in decisions if d['kept']] == ['a']
    assert future_labels[decisions[0]['opportunity_id']] == 'INVALID_AT_OPEN'


def test_missing_future_does_not_replace():
    assert reasons([frame(1), frame(2, 'b')]) == ['KEEP', 'EPISODE_SLOT_ALREADY_CONSUMED']
    assert 'complete48' not in Frame.__dataclass_fields__


def test_direct_flip():
    decisions, episodes = filter_frames([frame(1), frame(2, 'b', 'bear')])
    assert all(d['kept'] for d in decisions)
    assert episodes[0]['end_known'] and not episodes[1]['end_known']


def test_none_reentry():
    assert reasons([frame(1), frame(2, '', 'none'), frame(3, 'b')]) == ['KEEP', 'KEEP']


def test_session_break_no_reset():
    later = replace(frame(2, 'b'), eob='2026-01-06T00:15:00+08:00')
    assert reasons([frame(1), later]) == ['KEEP', 'EPISODE_SLOT_ALREADY_CONSUMED']


def test_roll_separate_leg_and_reselection_retains_slot():
    new = replace(frame(2, 'b'), actual_contract='DCE.m2609')
    assert reasons([frame(1), new, frame(3, 'c')]) == ['KEEP', 'KEEP', 'EPISODE_SLOT_ALREADY_CONSUMED']


def test_left_censored_stays_unknown_until_observed_transition():
    first = replace(frame(1), previous_state_verified=False, previous_direction=None)
    assert reasons([first, frame(2, 'b'), frame(3, 'c', 'bear')]) == [
        'LEFT_CENSORED_FIRST_UNKNOWN', 'LEFT_CENSORED_FIRST_UNKNOWN', 'KEEP']


def test_interrupted_prefix_is_not_session_break():
    assert reasons([frame(1), replace(frame(2, 'b'), contiguous=False), frame(3, 'c')]) == [
        'KEEP', 'LEFT_CENSORED_FIRST_UNKNOWN', 'LEFT_CENSORED_FIRST_UNKNOWN']


def test_verified_presample_first_consumes_slot():
    assert reasons([replace(frame(1), in_scoring_window=False), frame(2, 'b')]) == [
        'PRE_SAMPLE_CONSUMED', 'EPISODE_SLOT_ALREADY_CONSUMED']


def test_future_suffix_price_change_cannot_change_prefix():
    prefix = [frame(1), frame(2, 'b')]
    a = filter_frames(prefix+[frame(3, 'c')])[0]
    b = filter_frames(prefix+[frame(3, 'd', 'bear')])[0]
    assert a[:2] == b[:2]
    assert a[0]['kept'] and not a[1]['kept']


def test_closed_core_adapter_ignores_future_and_preserves_v6_identity():
    core = dict(close=100., atr14=2., trend_decision=dict(state=dict(trend_state='bull')),
        entry_evaluation=dict(accepted=True, path='D', evidence_id='e1', anchor=dict(anchor_price=98.)))
    row = dict(groups=dict(S0=core), decision_eob='2026-01-05T09:15:00+08:00', selected=True,
               complete_m15=True, labels={'48': 'TARGET_FIRST'}, next_open=100.)
    args = dict(tick=1., rules=dict(stop_buffer_atr=.6, stop_min_atr=1.5, stop_max_atr=6.),
                in_scoring_window=True, previous_state_verified=True, previous_direction='none')
    a = frame_from_v6(dict(csymbol='DCE.M', symbol='DCE.m2605'), row, 1, **args)
    row.update(labels={'48': 'CENSORED'}, next_open=-999.)
    b = frame_from_v6(dict(csymbol='DCE.M', symbol='DCE.m2605'), row, 1, **args)
    assert a == b
    assert a.evidence[0].opportunity_id == 'DCE.M|DCE.m2605|bull|D|e1'
    assert reasons([a]) == ['KEEP']


def test_deterministic_order_and_repeat_run():
    frames = [frame(2, 'b'), frame(1)]
    assert filter_frames(frames) == filter_frames(list(reversed(frames)))
