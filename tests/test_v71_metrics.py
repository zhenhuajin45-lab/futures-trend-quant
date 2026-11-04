from pathlib import Path
import pytest
from backtest.forward_v3 import write
from tools.first_entry_v71.metrics import evaluate
from tools.first_entry_v71.clock import AdmissionError,object_hash
from tools.first_entry_v71.scoring import score_once


def label(value):
    return dict(label=value,complete=value not in ('CENSORED','INVALID_AT_OPEN'))


def sample(first='TARGET_FIRST',later='STOP_FIRST',control='STOP_FIRST',n=20):
    rows=[];episodes=[];controls={};days=[f'2026-03-{d:02d}' for d in range(2,2+n)]
    for i,day in enumerate(days):
        episode=f'episode{i}'
        a,b=f'first{i}',f'later{i}'
        for key,value,selected in ((a,first,True),(b,later,False)):
            rows.append(dict(opportunity_id=key,csymbol='DCE.M',actual_contract='DCE.m.SYNTHETIC',trading_date=day,
                episode_id=episode,first_selected=selected,structural=label(value),symmetric=label(value)))
            controls[key]=label(control)
        episodes.append(dict(episode_id=episode,start_known=True,end_known=True,selected_key=a))
    return rows,episodes,controls,days,n


def test_all_registered_gates_pass_on_known_synthetic_population():
    summary,episodes,pairs=evaluate(*sample())
    assert summary['quality']['difference']==.5
    assert summary['capacity']['retention']==1
    assert summary['ordinary']['comparison']['paired_cells']['signal_only']==20
    assert summary['verdict']['decision']=='RETAIN_RESEARCH_CANDIDATE'
    assert len(episodes)==20 and len(pairs)==40


def test_quality_failure_archives():
    summary,_,_=evaluate(*sample('STOP_FIRST','TARGET_FIRST'))
    assert summary['verdict']['decision']=='ARCHIVE_FILTER_ROUTE'


def test_improves_s0_but_not_ordinary_is_not_promoted():
    summary,_,_=evaluate(*sample(control='TARGET_FIRST'))
    assert summary['verdict']['decision']=='INSUFFICIENT_EVIDENCE'
    assert summary['verdict']['relative_improvement_only']


def test_capacity_failure_despite_quality_improvement():
    rows=[];episodes=[];controls={};days=[f'2026-03-{d:02d}' for d in range(2,22)]
    for day in days:
        for j in range(5):
            episode=day+'_'+str(j);first=episode+'_0'
            for k in range(10):
                key=episode+'_'+str(k);won=(j<2 and k==0) or (j>=2 and k==1)
                rows.append(dict(opportunity_id=key,csymbol='DCE.M',actual_contract='DCE.M.SYNTHETIC',trading_date=day,
                    episode_id=episode,first_selected=k==0,structural=label('TARGET_FIRST' if won else 'STOP_FIRST'),
                    symmetric=label('TARGET_FIRST' if won else 'STOP_FIRST')))
                controls[key]=label('STOP_FIRST')
            episodes.append(dict(episode_id=episode,start_known=True,end_known=True,selected_key=first))
    summary,_,_=evaluate(rows,episodes,controls,days,len(days))
    assert summary['quality']['difference']>.29 and summary['capacity']['retention']==.4
    assert summary['verdict']['decision']=='ARCHIVE_FILTER_ROUTE'


def test_missing_first_does_not_gain_later_target_in_capacity():
    summary,table,_=evaluate(*sample('CENSORED','TARGET_FIRST'))
    assert summary['capacity']['valuable_complete_episodes']==0
    assert summary['capacity']['captured_complete_episodes']==0
    assert all(not r['eligible'] for r in table)
    assert summary['verdict']['decision']=='INSUFFICIENT_EVIDENCE'


def test_low_match_coverage_blocks_ordinary_claim():
    rows,episodes,controls,days,n=sample()
    controls={key:value for key,value in controls.items() if key in {'first'+str(i) for i in range(9)}}
    summary,_,_=evaluate(rows,episodes,controls,days,n)
    assert summary['ordinary']['match_coverage']==.45
    assert not summary['verdict']['promoted']


def test_incomplete_pairs_cannot_inflate_complete_pair_claim():
    rows,episodes,controls,days,n=sample()
    for i in range(0,n,2):
        next(r for r in rows if r['opportunity_id']=='first'+str(i))['symmetric']=label('STOP_FIRST')
        controls['first'+str(i)]=label('CENSORED')
    summary,_,_=evaluate(rows,episodes,controls,days,n)
    assert summary['ordinary']['comparison']['target_difference']['difference']==1
    assert summary['ordinary']['all_matched_censor_bounds']['lower']==0
    assert not summary['verdict']['promoted']


def test_nested_uncertainty_bounds_share_selected_outcomes():
    from tools.first_entry_v71.metrics import nested_quality
    rows,_,_,days,_=sample()
    rows[0]['structural']=label('BOTH_SAME_M1')
    result=nested_quality(rows,days)
    assert result['all_slot_censor_bounds']['lower']<=result['difference']<=result['all_slot_censor_bounds']['upper']


def test_boundary_episode_is_separate_and_can_block_gate():
    rows,episodes,controls,days,n=sample()
    episodes[0].update(start_known=False,end_known=False,selected_key='prior_sample_key')
    summary,table,_=evaluate(rows,episodes,controls,days,n)
    reasons=table[0]['exclusion_reasons']
    assert {'LEFT_CENSORED','OPEN_END','PRE_SAMPLE_CONSUMED'}<=set(reasons)
    assert summary['capacity']['valuable_complete_episodes']==19


def test_zero_opportunities_is_insufficient_not_positive():
    summary,table,pairs=evaluate([],[],{},[f'2026-03-{d:02d}' for d in range(2,22)],20)
    assert summary['verdict']['decision']=='INSUFFICIENT_EVIDENCE'
    assert not table and not pairs


def test_block_replay_is_deterministic_and_joint():
    a=evaluate(*sample());b=evaluate(*sample())
    assert a==b and a[0]['quality']['block5']['replicates']==10000


def test_cross_episode_diagnostic_keeps_frozen_first_touch_label():
    from tools.first_entry_v71.scoring import episode_boundary_diagnostics
    episode=dict(end_known=True,end_eob='2026-01-05T10:30:00+08:00')
    row=dict(structural=dict(label='TARGET_FIRST',entry_bob='2026-01-05T09:15:00+08:00',first_touch_eob='2026-01-05T10:30:00+08:00'))
    episode_boundary_diagnostics(row,episode,'2026-01-07T15:00:00+08:00')
    assert row['label_window_crosses_episode_end'] and row['first_touch_after_episode_end'] is False
    row['structural']['first_touch_eob']='2026-01-05T10:31:00+08:00'
    episode_boundary_diagnostics(row,episode,'2026-01-07T15:00:00+08:00')
    assert row['first_touch_after_episode_end'] and row['structural']['label']=='TARGET_FIRST'


def test_tampered_selection_rejected_before_future_reads(tmp_path):
    selection=tmp_path/'selection';selection.mkdir()
    write(selection/'opportunities.json',[dict(opportunity_id='changed')])
    lock=dict(purpose='ENGINEERING_FIXTURE_ONLY')
    write(selection/'receipt.json',dict(sample_lock_sha256=object_hash(lock),selection_object_sha256={'opportunities':object_hash([])}))
    with pytest.raises(AdmissionError,match='SELECTION_KEYS_CHANGED'):
        score_once({'lock':lock},tmp_path)
    assert not (tmp_path/'score').exists()


def test_second_score_claim_rejected_before_outcomes(tmp_path):
    selection=tmp_path/'selection';selection.mkdir();(tmp_path/'score').mkdir()
    lock=dict(purpose='ENGINEERING_FIXTURE_ONLY')
    write(selection/'receipt.json',dict(sample_lock_sha256=object_hash(lock),selection_object_sha256={}))
    with pytest.raises(AdmissionError,match='SCORE_ALREADY_CLAIMED'):
        score_once({'lock':lock},tmp_path)


def test_new_output_cannot_repeat_real_sample_score(tmp_path,monkeypatch):
    import tools.first_entry_v71.scoring as scoring
    monkeypatch.setattr(scoring,'ROOT',tmp_path)
    lock=dict(purpose='INDEPENDENT_RESEARCH',registration_sha256='registered-study-key')
    claims=tmp_path/'data/cache/v71_score_claims';claims.mkdir(parents=True)
    write(claims/'registered-study-key.json',dict(status='CLAIMED_BEFORE_LABELS'))
    lock['snapshot_revision']='changed-input-still-same-study'
    output=tmp_path/'new_output';selection=output/'selection';selection.mkdir(parents=True)
    write(selection/'receipt.json',dict(sample_lock_sha256=object_hash(lock),selection_object_sha256={}))
    with pytest.raises(AdmissionError,match='SAMPLE_ALREADY_SCORED_OR_CLAIMED'):
        score_once({'lock':lock},output)
    assert not (output/'score/receipt.json').exists()
