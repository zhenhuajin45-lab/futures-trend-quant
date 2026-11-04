"""Independent-history admission gate. No price labels or old-window fallback."""
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backtest.forward_v3 import read, write, digest
from tools.historical_strategy.reproduce import canonical_digest


def validate_window(start, scoring_end, path_end, old_start, old_end, forward_start):
    start, scoring_end, path_end, old_start, old_end, forward_start = map(
        datetime.fromisoformat, (start, scoring_end, path_end, old_start, old_end, forward_start))
    if any(t.tzinfo is None for t in (start, scoring_end, path_end, old_start, old_end, forward_start)):
        raise ValueError('timezone missing')
    if not start <= scoring_end <= path_end:
        raise ValueError('reversed sample or future endpoint')
    if start <= old_end and path_end >= old_start:
        raise ValueError('old research interval overlaps anchors or future price path')
    if path_end >= forward_start:
        raise ValueError('reserved formal-forward interval')


def require_qualified(manifest, registration, sample_lock=None):
    """Call before opening any new-sample candidate or outcome stream."""
    from tools.first_entry_v71.admission import declaration, DEFAULT_LOCK
    return declaration(manifest,registration,read(DEFAULT_LOCK) if sample_lock is None else sample_lock)


def main():
    registration = read(ROOT/'tools/first_entry_v7/registration.json')
    inventory_path = ROOT/'data/cache/first_entry_v7_source/inventory.json'
    inventory = read(inventory_path)
    probe = read(ROOT/'reports/v7_source_probe.json')
    # Inventory contains only eob/bob extrema and mapping dates, no price outcomes.
    summary = {name: dict(files=len(rows), first=min(r['min'] for r in rows), last=max(r['max'] for r in rows),
        minute_files=sum(r['column'] in ('eob', 'bob') for r in rows)) for name, rows in inventory.items()}
    products = {name: dict(status='NOT_AVAILABLE_IN_EXISTING_CACHE',
        proposed_scoring_trade_dates=registration['new_sample']['proposed_scoring_trade_dates'],
        qualified_past_complete_m15=0, minute_history='MISSING', actual_contract_inventory='MISSING',
        calendar_holiday_evidence='MISSING', schedule_sha256=None, selection_sha256=None,
        historical_available_at='UNKNOWN', historical_mapping_known_at='UNKNOWN')
        for name in registration['new_sample']['universe']}
    result = dict(status='READY_FOR_DATA', proposed_sample_only=True, no_new_sample_labels_opened=True,
        selector_run_on_old_window=False, products=products, existing_inventory=summary,
        private_inventory_sha256=digest(inventory_path), source_probe=probe,
        probe_scope='ONE_ACTUAL_CONTRACT_AND_FIVE_MINUTES; NOT_AN_ALL_PRODUCT_PERMISSION_CENSUS',
        next_physical_deliverable='12-product independent actual-contract M1 plus past warmup, historical calendar '
            'and day-selection proxy evidence; freeze manifest hashes before labels',
        no_fallback_to_used_or_forward_windows=True)
    write(ROOT/'reports/v7_new_sample_qualification.json', result)
    old = ROOT/'data/cache/history_v6_unadjusted'
    hashes = {p.name: digest(p) for p in sorted(old.glob('*_schedule.json'))+sorted(old.glob('*_selection.json'))}
    write(ROOT/'reports/v7_input_clock_hashes.json', dict(v6_existing_clock_sha256=hashes,
        note='Old P0 diagnostic inputs only; these are NOT qualified new-sample clocks',
        production_sources='Inherited V6 canonical_source_sha256 in reports/v6_reproduction_lock.json',
        registration_sha256=canonical_digest(ROOT/'tools/first_entry_v7/registration.json')))
    print('V7_READY_FOR_DATA', len(products), 'products; independent sample scoring NOT RUN')


if __name__ == '__main__':
    main()
