"""One registered scale diagnostic; fixed V6 pairing, no strategy selection."""
import argparse
from collections import Counter, defaultdict
from pathlib import Path
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backtest.forward_v3 import read, write, digest, check_lock
from tools.historical_strategy.reproduce import check_sources
from tools.historical_strategy.analyze import interval, distribution
from tools.historical_strategy.price_paths import label_opportunity, ExpectedClock


def comparison(rows, days):
    panel = defaultdict(lambda: [0, 0])
    stop_panel = defaultdict(lambda: [0, 0])
    optimistic = defaultdict(lambda: [0, 0])
    bounds = [defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])]
    cross = Counter()
    for day, a, b in rows:
        ta, tb = a == 'TARGET_FIRST', b == 'TARGET_FIRST'
        ba, bb = a == 'BOTH_SAME_M1', b == 'BOTH_SAME_M1'
        cross['both' if ta and tb else 'signal_only' if ta else 'control_only' if tb else 'neither'] += 1
        for values, value in ((panel, int(ta)-int(tb)),
                              (stop_panel, int(a == 'STOP_FIRST')-int(b == 'STOP_FIRST')),
                              (optimistic, int(ta or ba)-int(tb or bb)),
                              (bounds[0], int(ta)-int(tb or bb)),
                              (bounds[1], int(ta or ba)-int(tb))):
            values[day][0] += value
            values[day][1] += 1
    def result(p):
        numerator = sum(x[0] for x in p.values())
        return dict(numerator=numerator, denominator=len(rows), difference=numerator/len(rows),
                    block5=interval(p, days, 5), block10=interval(p, days, 10))
    return dict(signal_target=sum(a == 'TARGET_FIRST' for _, a, _ in rows),
                control_target=sum(b == 'TARGET_FIRST' for _, _, b in rows), paired_cells=dict(cross),
                target_difference=result(panel), stop_difference=result(stop_panel),
                both_optimistic=result(optimistic), ambiguous_lower=result(bounds[0]), ambiguous_upper=result(bounds[1]),
                signal_labels=dict(Counter(a for _, a, _ in rows)), control_labels=dict(Counter(b for _, _, b in rows)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT/'data/cache/first_entry_v7_scale')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    check_lock()
    lock = read(ROOT/'reports/v6_reproduction_lock.json')
    check_sources(lock)
    source = ROOT/'data/cache/history_v6_final'
    immutable = [source/'opportunities.json', source/'controls.json', ROOT/'reports/v6_opportunity_pairs.csv',
                 ROOT/'reports/v6_price_events_summary.json', ROOT/'reports/v6_reproduction_lock.json']
    data = ROOT/'data/cache/history_v6_unadjusted'
    immutable += sorted(data.glob('*_schedule.json')) + sorted(data.glob('*_selection.json'))
    before = {str(p.relative_to(ROOT)): digest(p) for p in immutable}
    assert digest(data/'manifest.json') == lock['data_manifest_sha256']
    for name, expected in lock['data_file_sha256'].items():
        assert digest(data/name) == expected, name
    signals, controls = read(source/'opportunities.json'), read(source/'controls.json')
    rules = read(ROOT/'config/strategy.json')['rules']
    fees = read(ROOT/'config/fees.json')['products']
    matched = {r['opportunity_id']: r for r in controls}
    all_rows = signals + controls
    by_contract = defaultdict(list)
    for row in all_rows:
        by_contract[(row['csymbol'], row['actual_contract'])].append(row)
    days = set()
    for (csymbol, actual), rows in sorted(by_contract.items()):
        schedule = read(data/(csymbol+'_schedule.json'))
        expected = [pd.Timestamp(x['eob']) for x in schedule]
        days.update(x['trading_date'] for x in schedule)
        raw = pd.read_parquet(data/'contracts'/(actual.replace('.', '_')+'.parquet')).sort_values('bob')
        records = raw.to_dict('records')
        indexed = dict(list=records, bobs=list(raw.bob), by_eob={x['eob']: x for x in records},
                       clock_index=ExpectedClock(expected))
        tick = fees[csymbol.split('.')[1]]['price_tick']
        for row in rows:
            signal = dict(row, time=pd.Timestamp(row['time']))
            row['symmetric48'] = label_opportunity(signal, indexed, expected, tick, rules, 48, risk_atr=1.5)
            new = row['symmetric48']
            if new.get('r') is not None:
                error = new['r']-1.5*row['atr']
                assert -1e-7 <= error <= tick+1e-7
        print(actual, len(rows), flush=True)
    pairs, changed, rounding, old_r_delta = [], [], [], []
    earlier_controls = 0
    primary_ids = []
    old_rows, new_rows = [], []
    immediate = []
    for signal in signals:
        control = matched.get(signal['opportunity_id'])
        if control is None:
            continue
        eligible = signal['labels']['48']['complete'] and control['labels']['48']['complete']
        item = dict(opportunity_id=signal['opportunity_id'], csymbol=signal['csymbol'],
                    actual_contract=signal['actual_contract'], signal_time=signal['time'], control_time=control['time'],
                    trading_date=signal['trading_date'], original_primary_eligible=eligible)
        for arm, row in (('signal', signal), ('control', control)):
            old, new = row['labels']['48'], row['symmetric48']
            item[arm] = {prefix+'_'+key: label.get(key) for prefix, label in (('old', old), ('new', new))
                         for key in ('label', 'complete', 'first_touch_eob', 'r_over_atr', 'entry_eob')}
            if new.get('r') is not None:
                item[arm]['rounding_error_atr'] = new['r_over_atr']-1.5
                if eligible:
                    rounding.append(dict(arm=arm, error_atr=new['r_over_atr']-1.5))
        pairs.append(item)
        if eligible:
            assert signal['symmetric48']['complete'] and control['symmetric48']['complete']
            primary_ids.append(signal['opportunity_id'])
            old_rows.append((signal['trading_date'], signal['labels']['48']['label'], control['labels']['48']['label']))
            new_rows.append((signal['trading_date'], signal['symmetric48']['label'], control['symmetric48']['label']))
            old_r_delta.append(control['labels']['48']['r_over_atr']-signal['labels']['48']['r_over_atr'])
            earlier_controls += pd.Timestamp(control['labels']['48']['entry_eob']) < pd.Timestamp(signal['labels']['48']['entry_eob'])
            if all(pd.Timestamp(r['labels']['48']['entry_eob']) == pd.Timestamp(r['time'])+pd.Timedelta(minutes=1)
                   for r in (signal, control)):
                immediate.append(new_rows[-1])
        if any(item[arm]['old_label'] != item[arm]['new_label'] for arm in ('signal', 'control')):
            changed.append(item)
    assert len(primary_ids) == 2493
    days = sorted(days)
    old, new = comparison(old_rows, days), comparison(new_rows, days)
    assert old['paired_cells'] == dict(both=736, signal_only=92, control_only=157, neither=1508)
    main = new['target_difference']
    sensitive = main['difference'] > 0 or any(main[k]['low'] <= 0 <= main[k]['high'] for k in ('block5', 'block10'))
    episode_key = lambda r: (r['csymbol'], r['actual_contract'], r['episode'])
    winning = [r for r in signals if r['labels']['48']['complete'] and r['labels']['48']['label'] == 'TARGET_FIRST']
    ceiling = dict(opportunities=len(signals), episodes=len({episode_key(r) for r in signals}),
                   target_events=len(winning), target_episodes=len({episode_key(r) for r in winning}))
    assert ceiling == dict(opportunities=3787, episodes=284, target_events=1097, target_episodes=138)
    summary = dict(study='V7_P0_SYMMETRIC_ONLY', registered_before_run='fb7b9eb', original_primary_pairs=2493,
                   original_pair_keys_retained=len(pairs), original_exclusions_retained=len(pairs)-2493,
                   old=old, symmetric=new, immediate_entry_symmetric=comparison(immediate, days),
                   original_controls_entering_earlier=earlier_controls,
                   old_control_minus_signal_r_atr=dict(distribution(old_r_delta), minimum=min(old_r_delta), maximum=max(old_r_delta)),
                   rounding_error_atr={arm: dict(distribution([r['error_atr'] for r in rounding if r['arm'] == arm]),
                        minimum=min(r['error_atr'] for r in rounding if r['arm'] == arm),
                        maximum=max(r['error_atr'] for r in rounding if r['arm'] == arm))
                                       for arm in ('signal', 'control')},
                   primary_label_transitions={arm: dict(Counter(r[arm]['old_label']+' -> '+r[arm]['new_label']
                        for r in pairs if r['original_primary_eligible'])) for arm in ('signal', 'control')},
                   changed_pairs_all=len(changed), changed_pairs_primary=sum(r['original_primary_eligible'] for r in changed),
                   capacity_upper_bound_only=ceiling, selector_run_on_old_window=False,
                   pure_timing_claim='SCALE_SENSITIVE' if sensitive else 'NEGATIVE_UNDER_BOTH_REGISTERED_SCALES',
                   m5='STOPPED', no_orders=True, no_net_returns=True, immutable_v6_sha256=before,
                   registration_sha256=digest(ROOT/'tools/first_entry_v7/registration.json'))
    assert before == {str(p.relative_to(ROOT)): digest(p) for p in immutable}
    write(args.output/'private_symmetric_signals.json', signals)
    write(args.output/'private_symmetric_controls.json', controls)
    write(ROOT/'reports/v7_symmetric_pairs.json', dict(events=pairs))
    write(ROOT/'reports/v7_symmetric_changed_pairs.json', dict(events=changed))
    pd.json_normalize(pairs, sep='_').to_csv(ROOT/'reports/v7_symmetric_pairs.csv', index=False)
    write(ROOT/'reports/v7_symmetric_summary.json', summary)
    print('V7_SYMMETRIC_PASS', new['target_difference'], summary['pure_timing_claim'], flush=True)


if __name__ == '__main__':
    main()
