"""Guarded offline selector entry. Requires a qualified independent manifest first."""
import argparse
import gzip
from pathlib import Path
import sys
from bisect import bisect_left
from collections import defaultdict
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backtest.forward_v3 import read, write, digest, stamp
from tools.first_entry_v7.source_qualification import require_qualified
from tools.first_entry_v7.selector import frame_from_v6, filter_frames
from tools.historical_strategy.reproduce import check_sources, canonical_digest


def complete_past_ends(actual_eobs, expected_eobs):
    """Count actual full warmup, including early bars not emitted by core replay."""
    if len(actual_eobs) != len(set(actual_eobs)):
        raise ValueError('duplicate raw minute')
    actual, expected = defaultdict(set), defaultdict(set)
    for source, times in ((actual, actual_eobs), (expected, expected_eobs)):
        for time in times:
            source[time.ceil('15min')].add(time)
    return sorted(end for end, times in expected.items()
                  if actual.get(end) == times and max(times) == end)


def run(manifest_path, input_root, output):
    manifest = read(manifest_path)
    registration = read(ROOT/'tools/first_entry_v7/registration.json')
    require_qualified(manifest, registration)  # Before replay paths or labels are accessed.
    check_sources(read(ROOT/'reports/v6_reproduction_lock.json'))
    if manifest.get('registration_sha256') != canonical_digest(ROOT/'tools/first_entry_v7/registration.json'):
        raise ValueError('registered policy hash mismatch')
    rules = read(ROOT/'config/strategy.json')['rules']
    fees = read(ROOT/'config/fees.json')['products']
    start, end = stamp(manifest['first_scoring_eob']), stamp(manifest['last_scoring_eob'])
    frames = []
    for csymbol, product in sorted(manifest['products'].items()):
        def verified(relative, expected):
            path = (input_root/relative).resolve()
            if not path.is_relative_to(input_root.resolve()) or digest(path) != expected:
                raise ValueError('input file outside snapshot or hash mismatch')
            return path
        schedule_path = verified(product['schedule_path'], product['schedule_sha256'])
        selection = read(verified(product['selection_path'], product['selection_sha256']))
        verified(product['calendar_evidence_path'], product['calendar_evidence_sha256'])
        schedule = read(schedule_path)
        expected_eobs = [stamp(r['eob']) for r in schedule]
        scored_days = {r['trading_date'] for r in schedule if start < stamp(r['eob']) <= end}
        registered_legs = {c['symbol'] for c in product['contracts']}
        if any(selection.get(day) not in registered_legs for day in scored_days):
            raise ValueError('selected actual contract absent from qualified inventory')
        boundaries = sorted({time.ceil('15min') for time in expected_eobs})
        ordinal = {time: i for i, time in enumerate(boundaries)}
        for contract in product['contracts']:
            raw = verified(contract['raw_path'], contract['raw_sha256'])
            complete_ends = complete_past_ends(list(pd.read_parquet(raw, columns=['eob']).eob), expected_eobs)
            replay = verified(contract['replay_path'], contract['replay_sha256'])
            previous = None
            previous_state = None
            v6_episode = 0
            with gzip.open(replay, 'rt', encoding='utf8') as stream:
                import json
                header = json.loads(next(stream))
                if (header['task']['symbol'] != contract['symbol'] or header['task']['csymbol'] != csymbol
                        or header['input_sha256'] != contract['raw_sha256']):
                    raise ValueError('actual contract replay identity mismatch')
                for index, line in enumerate(stream):
                    row = json.loads(line)
                    now = stamp(row['decision_eob'])
                    if now > end:
                        break  # Outcomes and later prices never enter selection.
                    scoring = start <= now <= end
                    if scoring and bisect_left(complete_ends, now) < 1200:
                        raise ValueError('insufficient actual-contract past complete warmup')
                    contiguous = previous is None or (now in ordinal and previous in ordinal and ordinal[now]-ordinal[previous] == 1)
                    state = row['groups']['S0']['trend_decision']['state']['trend_state']
                    if row['selected'] and selection.get(row['trading_date']) != contract['symbol']:
                        raise ValueError('replay selected flag disagrees with frozen actual-contract mapping')
                    v6_episode += state != previous_state
                    frames.append(frame_from_v6(dict(csymbol=csymbol, symbol=contract['symbol']), row, index,
                        fees[csymbol.split('.')[1]]['price_tick'], rules, in_scoring_window=scoring,
                        contiguous=contiguous, v6_episode=v6_episode))
                    previous = now
                    previous_state = state
    decisions, episodes = filter_frames(frames)
    output.mkdir(parents=True, exist_ok=False)
    write(output/'decisions.json', decisions)
    write(output/'episodes.json', episodes)
    write(output/'receipt.json', dict(stage='SELECTION_ONLY; NO_PRICE_LABELS_SCORED',
        manifest_sha256=digest(manifest_path), retained=sum(r['kept'] for r in decisions),
        registration_sha256=canonical_digest(ROOT/'tools/first_entry_v7/registration.json'), no_orders=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--input-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.manifest, args.input_root, args.output)


if __name__ == '__main__':
    main()
