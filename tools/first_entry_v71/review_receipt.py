"""Package current synthetic checks and raw coverage; no real research scoring."""
import importlib.util
from pathlib import Path
import pandas as pd
from backtest.forward_v3 import read,write,digest,check_lock
from tools.historical_strategy.reproduce import canonical_digest,check_sources
from tools.first_entry_v71.closeout import cases
from tools.first_entry_v71.clock import NIGHT_END

ROOT=Path(__file__).resolve().parents[2]
BASELINE='bc54d7af15fccaca53ba5989f84e0436d4c0de78'
VERDICT_FIX='c9ab8af05efbb315390bf9874f20e60e969196e4'


def main():
    logic=cases(ROOT/'data/cache/v711_unit_junit.xml')
    contracts=cases(ROOT/'data/cache/v711_data_unit_junit.xml')
    assert len(logic)==70 and len(contracts)==4
    old=read(ROOT/'reports/v71_engineering_checks.json')
    changed=[name for name,sha in old['source_canonical_sha256'].items() if canonical_digest(ROOT/name)!=sha]
    assert changed==['tools/first_entry_v71/metrics.py'],changed
    assert canonical_digest(ROOT/'tools/first_entry_v7/registration.json')==old['registration_sha256']
    check_lock();check_sources(read(ROOT/'reports/v6_reproduction_lock.json'))
    assert read(ROOT/'config/strategy.json')['execution']['execute_enabled'] is False
    assert read(ROOT/'config/v71_sample_lock.json')['status']=='READY_FOR_DATA'
    assert not list((ROOT/'data/cache/v71_score_claims').glob('*.json'))
    before=ROOT/'data/cache/v711_verdict_red_before.txt'
    text=before.read_text(encoding='utf8',errors='replace')
    assert '1 failed' in text and 'RETAIN_RESEARCH_CANDIDATE' in text
    # Capture the exact supplied regression's call into the actual scoring function.
    spec=importlib.util.spec_from_file_location('v711_capture_case',ROOT/'tests/test_v71_metrics.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    captured={};evaluate=module.evaluate
    def capture(*args):
        result=evaluate(*args);captured.update(summary=result[0],rows=args[0]);return result
    module.evaluate=capture
    module.test_all_slots_zero_advantage_blocks_complete_only_promotion()
    summary=captured['summary'];rows=captured['rows'];q=summary['quality']
    data=read(ROOT/'reports/v711_warmup_receipts.json')
    assert all(r['qualified_complete_m15']==0 and not r['proof_of_1200_qualified_m15'] for r in data['legs'])
    previous_cli=read(ROOT/'reports/v71_cli_checks.json')
    assert len(previous_cli['cases'])==19 and all(r['status']=='PASS' for r in previous_cli['cases'])
    fingerprint_paths=['tools/first_entry_v71/metrics.py','tools/first_entry_v71/warmup_receipts.py',
        'tools/first_entry_v71/review_receipt.py','tests/test_v71_metrics.py','tests/test_v711_warmup_receipts.py',
        'tools/first_entry_v7/selector.py','tools/first_entry_v7/symmetric.py','config/fees.json',
        'config/strategy.json','config/v71_trade_date_lock.json']
    report=dict(status='PASS',scope='SYNTHETIC_VERDICT_AND_RAW_COVERAGE_ONLY',baseline_commit=BASELINE,
        verdict_fix_commit=VERDICT_FIX,task_patch_sha256=digest(ROOT/'docs/tasks/v71-verdict.patch'),
        reproduced_before=dict(observed_verdict='RETAIN_RESEARCH_CANDIDATE',expected_test_failures=1,log_sha256=digest(before)),
        after_patch=dict(verdict=summary['verdict'],complete_first=q['first'],complete_s0=q['s0'],
            complete_difference=q['difference'],all_slots_first=len([r for r in rows if r['first_selected']]),
            all_slots_s0=len(rows),all_slot_advantage_bounds=q['all_slot_censor_bounds'],
            valuable_episode_retention=summary['capacity']['retention'],ordinary_match_coverage=summary['ordinary']['match_coverage']),
        current_test_count=74,current_logic_tests=logic,current_raw_contract_tests=contracts,
        inherited_cli=dict(baseline_commit=BASELINE,count=19,rerun_this_iteration=False,
            source_receipt_canonical_sha256=canonical_digest(ROOT/'reports/v71_cli_checks.json')),
        old_v71_source_fingerprints='ARCHIVED_AT_BASELINE; current metrics fingerprint superseded by this receipt',
        current_source_canonical_sha256={name:canonical_digest(ROOT/name) for name in fingerprint_paths},
        registration_sha256=old['registration_sha256'],frozen_protocol='PASS',
        raw_coverage_receipt_canonical_sha256=canonical_digest(ROOT/'reports/v711_warmup_receipts.json'),
        real_sample_status='READY_FOR_DATA',real_sample_score_runs=0,orders_enabled=False,
        m5='STOPPED',fee_or_net_returns_evaluated=False,
        execution_path_verified='evaluate -> verdict -> summary; synthetic research path only')
    write(ROOT/'reports/v711_review_checks.json',report)
    # Keep the original 29-leg inventory immutable and annotate the newly observed boundary separately.
    inventory=read(ROOT/'reports/v71_actual_leg_data_gaps.json')['rows'];plan=[]
    for row in inventory:
        cs=row['csymbol'];night=NIGHT_END.get(cs)
        night_minutes=0
        if night:
            h,m=map(int,night.split(':'));night_minutes=(h*60+m-21*60)%(24*60)
        lost_day_bars=15+night_minutes//15
        upper=row['free_year_full_day_m15_upper_bound_before_first_day']
        projected=max(0,upper-lost_day_bars)
        plan.append(dict(csymbol=cs,actual_contract=row['actual_contract'],listed_at=row['listed_at'],
            first_selected_trade_date=row['first_selected_trade_date'],
            warmup_first_eob_proxy=row['earliest_required_warmup_eob_proxy'],
            fixed_label_cap=row['last_label_cap'],baseline_free_m15_upper=upper,
            baseline_older_m15_required=max(0,1200-upper),
            next_day_boundary_projected_upper=projected,projected_older_m15_required=max(0,1200-projected),
            projection_basis='PROXY_ONLY; JD2605 Nov04 returned403 and Nov05 returned200; other legs not re-probed at that boundary',
            required_m15=1200,qualified_m15=0,
            work_order='PRIORITY_REPRESENTATIVE' if row['actual_contract'] in ('DCE.jd2602','DCE.eg2605') else
                'BOUNDARY_PRESERVED_PARTIAL' if row['actual_contract']=='DCE.jd2605' else 'WAIT_FOR_TWO_REPRESENTATIVES',
            scoring_price_start='2026-01-05; selected actual leg only',scoring_price_end='2026-04-24',
            tail_requirement='48 M15 after each signal on SAME actual contract, including old roll leg, capped May06 15:00 +08',
            source_and_session_qualification='PENDING'))
    assert sum(r['baseline_older_m15_required']>0 for r in plan)==13
    assert sum(r['baseline_older_m15_required'] for r in plan)==4599
    pd.DataFrame(plan).to_csv(ROOT/'reports/v711_29_leg_fetch_plan.csv',index=False)
    write(ROOT/'reports/v711_29_leg_fetch_plan.json',dict(baseline_commit=BASELINE,actual_legs=29,
        baseline_short_legs=13,baseline_older_m15_required=4599,
        next_day_boundary_projected_short_legs=sum(r['projected_older_m15_required']>0 for r in plan),
        next_day_boundary_projected_older_m15_required=sum(r['projected_older_m15_required'] for r in plan),
        qualification='READY_FOR_DATA',projection_is_not_a_29_leg_permission_audit=True,rows=plan))
    print('V711_REVIEW_PASS 70 logic + 4 raw-contract tests; inherited CLI19 not rerun; real sample READY_FOR_DATA')


if __name__=='__main__':main()
