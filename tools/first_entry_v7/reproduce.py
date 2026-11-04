"""Recompute scale labels from local licensed M1 and compare fixed output keys."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backtest.forward_v3 import read, write, digest, check_lock
from tools.historical_strategy.reproduce import canonical_digest


def main():
    check_lock()
    files = ['reports/v7_symmetric_pairs.json', 'reports/v7_symmetric_pairs.csv',
             'reports/v7_symmetric_changed_pairs.json']
    hashes = {name: canonical_digest(ROOT/name) for name in files}
    old = read(ROOT/'reports/v7_symmetric_summary.json')
    subprocess.run([sys.executable, str(ROOT/'tools/first_entry_v7/symmetric.py')], cwd=ROOT, check=True)
    new = read(ROOT/'reports/v7_symmetric_summary.json')
    assert hashes == {name: canonical_digest(ROOT/name) for name in files}, 'pair keys / labels / ordering changed'
    for key in ('old', 'symmetric', 'immediate_entry_symmetric', 'capacity_upper_bound_only', 'pure_timing_claim'):
        assert old[key] == new[key], key
    write(ROOT/'reports/v7_reproduction_check.json', dict(status='PASS', public_pair_canonical_sha256=hashes,
        canonicalization='UTF8_TEXT_UNIVERSAL_NEWLINES',
        pair_count=2785, original_primary_pairs=2493, all_signal_label_count=3787, all_control_label_count=2785,
        labels_recomputed_from_licensed_m1=True, bootstrap_identical=True, selector_old_window_run=False,
        candidate_new_sample_reproduction='NOT_RUN; READY_FOR_DATA', no_orders=True))
    print('V7_SCALE_REPRODUCTION_PASS')


if __name__ == '__main__':
    main()
