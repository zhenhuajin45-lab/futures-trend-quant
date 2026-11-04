"""Publish only engineering receipts, synthetic tables and derived supply gaps."""
import argparse
from collections import Counter
from pathlib import Path
import shutil,sys
import xml.etree.ElementTree as ET
import pandas,numpy,pyarrow

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,digest,check_lock
from tools.historical_strategy.reproduce import canonical_digest,check_sources


def cases(path):
    root=ET.parse(path).getroot()
    result=[]
    for row in root.iter('testcase'):
        assert not list(row.iter('failure')) and not list(row.iter('error')) and not list(row.iter('skipped')),row.attrib
        result.append(dict(name=row.attrib['name'],status='PASS',seconds=float(row.attrib.get('time',0))))
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--pytest-root',type=Path,required=True)
    p.add_argument('--red-log',type=Path,default=ROOT.parent/'v71_red_before.txt');a=p.parse_args()
    unit=cases(ROOT/'data/cache/v71_unit_junit.xml');cli=cases(ROOT/'data/cache/v71_cli_junit.xml')
    assert len(unit)==69 and len(cli)==19
    diagnostics=[];positive=None;zero=None;later=None
    for folder in sorted(a.pytest_root.glob('test_*')):
        if not folder.is_dir() or folder.name.endswith('current'):continue
        for failure in folder.glob('*_failure.json'):
            row=read(failure)
            output=folder/failure.name.removesuffix('_failure.json')
            assert not (output/'selection/receipt.json').exists()
            diagnostics.append(dict(case=folder.name,**row))
        for receipt in folder.glob('*/selection/receipt.json'):
            row=read(receipt);assert row['qualified_m15']==row['expected_m15']==21760
            if receipt.parent.parent.name=='positive':positive=receipt.parent.parent
            elif receipt.parent.parent.name=='zero':zero=receipt.parent.parent
            elif receipt.parent.parent.name=='later':later=receipt.parent.parent
    assert len(diagnostics)==16 and positive is not None and zero is not None and later is not None
    summary=read(positive/'score/summary.json')
    assert summary['purpose']=='ENGINEERING_FIXTURE_ONLY' and summary['verdict']['decision']=='INSUFFICIENT_EVIDENCE'
    for source,name in (('opportunity_table.csv','opportunities.csv'),('episode_table.csv','episodes.csv'),('ordinary_pair_table.csv','ordinary_pairs.csv')):
        shutil.copyfile(positive/'score'/source,ROOT/'reports'/('v71_fixture_'+name))
    write(ROOT/'reports/v71_fixture_score_summary.json',summary)
    write(ROOT/'reports/v71_fixture_zero_summary.json',read(zero/'score/summary.json'))
    coverage=read(positive/'selection/coverage.json')
    per_product={cs:dict(expected=sum(r['expected_m15'] for r in coverage if r['csymbol']==cs),
        actual=sum(r['actual_frames'] for r in coverage if r['csymbol']==cs),
        qualified=sum(r['qualified_frames'] for r in coverage if r['csymbol']==cs)) for cs in sorted({r['csymbol'] for r in coverage})}
    write(ROOT/'reports/v71_cli_checks.json',dict(status='PASS',purpose='ENGINEERING_FIXTURE_ONLY',
        cli_command='python -m tools.first_entry_v71.entrypoint --manifest <FIXTURE>/manifest.json --input-root <FIXTURE> '
            '--sample-lock <FIXTURE>/sample_lock.json --output <OUTPUT> --fixture [--score]',
        cases=cli,negative_diagnostics=diagnostics,positive_coverage_by_product=per_product,
        full_coverage_zero_opportunities=read(zero/'selection/receipt.json'),
        unselected_leg_later_warmup_pass=read(later/'selection/receipt.json'),
        failed_inputs_success_receipts=0,real_sample_labels_read=False))
    metadata=read(ROOT/'data/cache/v71_source_inventory/metadata.json')
    entitlement=read(ROOT/'data/cache/v71_source_inventory/entitlement_check.json')
    write(ROOT/'reports/v71_source_supply_checks.json',dict(status='READY_FOR_DATA',
        metadata_source_sha256=digest(ROOT/'data/cache/v71_source_inventory/metadata.json'),
        historical_clock_only_probes=metadata['probes'],existing_environment_entitlement=entitlement,
        no_real_scoring_window_price_request_in_v71=True,historical_arrival_at='UNKNOWN',historical_mapping_known_at='UNKNOWN'))
    check_lock();check_sources(read(ROOT/'reports/v6_reproduction_lock.json'))
    for name,expected in read(ROOT/'reports/v7_reproduction_check.json')['public_pair_canonical_sha256'].items():
        assert canonical_digest(ROOT/name)==expected,name
    assert read(ROOT/'config/strategy.json')['execution']['execute_enabled'] is False
    assert read(ROOT/'config/v71_sample_lock.json')['status']=='READY_FOR_DATA'
    red_path=a.red_log
    red=red_path.read_text(encoding='utf8',errors='replace')
    assert '3 failed' in red
    sources=sorted((ROOT/'tools/first_entry_v71').glob('*.py'))+[ROOT/'tools/first_entry_v7/run_selector.py',ROOT/'tools/first_entry_v7/source_qualification.py']
    write(ROOT/'reports/v71_engineering_checks.json',dict(status='PASS',baseline='375d9c42bb9500635a3067e8163d601ea5babbb7',
        before_fix_red_regressions=3,before_fix_red_sha256=digest(red_path),after_fix_regressions='PASS',
        targeted_tests=len(unit)+len(cli),unit_tests=len(unit),normal_cli_tests=len(cli),negative_cli_cases=16,
        unit_cases=unit,production_core_unchanged=True,formal_protocol_hash='PASS',old_v7_paired_results_unchanged=True,
        environment=dict(python=sys.version.split()[0],pandas=pandas.__version__,numpy=numpy.__version__,pyarrow=pyarrow.__version__),
        commands=['python -m pytest tests/test_v71_metrics.py tests/test_v71_clock.py tests/test_v71_label_cap.py tests/test_v71_red_regressions.py '
            'tests/test_first_entry_v7.py tests/test_v7_admission.py tools/historical_strategy/test_price_paths.py '
            'tools/historical_strategy/test_opportunities.py -q --tb=short --junitxml=data/cache/v71_unit_junit.xml',
            'python -m pytest tests/test_v71_cli.py -q --tb=short --basetemp data/cache/v71_cli_persistent_final --junitxml=data/cache/v71_cli_junit.xml',
            'python -m compileall -q tools/first_entry_v71 tools/first_entry_v7/run_selector.py tools/first_entry_v7/source_qualification.py',
            'git diff --check'],
        initial_engineering_attempt='Full fixture hit 360-second test watchdog; stopped only identified synthetic workers. '
            'Admission now validates products sequentially, keeps verified paths/compact clocks instead of all raw frames, '
            'frees temporary schedules and uses vector time checks; score loads one leg after frozen selection; final watchdog 900s.',
        evidence_packaging='An earlier passing CLI run used pytest temporary storage later garbage-collected by another test run. '
            'Final CLI cases were rerun under data/cache/v71_cli_persistent_final; this report uses those actual receipts and final JUnit only.',
        execute_enabled=False,m5='STOPPED',real_sample_status='READY_FOR_DATA',real_sample_score_runs=0,
        registered_quality_delta=.02,registered_episode_retention=.5,registered_match_coverage=.7,
        source_canonical_sha256={p.relative_to(ROOT).as_posix():canonical_digest(p) for p in sources},
        registration_sha256=canonical_digest(ROOT/'tools/first_entry_v7/registration.json'),
        scope='OFFLINE_RESEARCH; NOT_PRODUCTION_EXECUTION_READINESS'))
    print('V71_THREE_DELIVERABLES_PASS',len(unit)+len(cli),'tests; REAL_SAMPLE_READY_FOR_DATA',flush=True)


if __name__=='__main__':main()
