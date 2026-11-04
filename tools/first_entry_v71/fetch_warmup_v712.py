"""Fetch two fixed pre-scoring EDB ranges. Python 3.9+, standard library only."""
import argparse
import csv
import hashlib
import io
from http.client import HTTPException
import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

RECEIPT_SHA = '3b1ddff678000812823424a04985c46d4ce32457a8c0d13dd7124e6c49e97b97'
CUT = '2025-11-05T15:00:00+08:00'
CN = timezone(timedelta(hours=8))
LEGS = {'DCE.jd2602': 9000, 'DCE.eg2605': 13800}
FULL = ('open', 'high', 'low', 'close', 'volume', 'open_oi', 'close_oi')
EDB = 'https://edb.shinnytech.com/md/kline'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def expected_minutes(leg):
    expected = set()
    for gap in leg['missing_intervals_proxy']:
        if gap['last_eob'] <= CUT:
            continue
        first = datetime.fromisoformat(gap['first_eob'])
        last = datetime.fromisoformat(gap['last_eob'])
        if gap['first_eob'] <= CUT or last != first + timedelta(minutes=gap['minutes'] - 1):
            raise ValueError('unexpected gap boundaries')
        expected.update(int(first.timestamp()) + 60 * i for i in range(gap['minutes']))
    if len(expected) != LEGS[leg['actual_contract']]:
        raise ValueError('gap inventory differs from the reviewed contract')
    return expected


def inspect_csv(data, expected, fields):
    rows = csv.DictReader(io.StringIO(data.decode('utf-8-sig')))
    if rows.fieldnames is None or not {'datetime_nano', *fields} <= set(rows.fieldnames):
        raise ValueError('missing EDB fields')
    observed = []
    zero = 0
    for row in rows:
        bob_ns = int(row['datetime_nano'])
        if bob_ns % 60_000_000_000:
            raise ValueError('timestamp is not minute-aligned')
        observed.append(bob_ns // 1_000_000_000 + 60)
        v = {f: Decimal(row[f]) for f in fields}
        if not all(n.is_finite() for n in v.values()) or v['volume'] < 0:
            raise ValueError('nonfinite value or negative volume')
        zero += v['volume'] == 0
        if fields == FULL:
            if min(v['open_oi'], v['close_oi']) < 0:
                raise ValueError('negative open interest')
            if not (v['low'] <= min(v['open'], v['close']) <= max(v['open'], v['close']) <= v['high']):
                raise ValueError('inconsistent OHLC')
    unique = set(observed)
    return dict(rows=len(observed), duplicates=len(observed)-len(unique),
                missing=len(expected-unique), extra=len(unique-expected),
                time_sorted=observed == sorted(observed), zero_volume_rows=zero)


def fetch(symbol, expected, fields, folder, item):
    def wall(second):
        return datetime.fromtimestamp(second, CN).strftime('%Y-%m-%d %H:%M:%S')
    params = dict(symbol=symbol, period=60, start_time=wall(min(expected)-60),
                  end_time=wall(max(expected)), col=','.join(fields))
    item.update(request=params, expected=len(expected), requested_at=datetime.now(timezone.utc).isoformat())
    request = Request(EDB + '?' + urlencode(params))
    try:
        with urlopen(request, timeout=60) as response:
            item['http_status'] = response.status
            data = response.read()
    except HTTPError as exc:
        item['http_status'] = exc.code
        data = exc.read()
        (folder / (symbol + '.error')).write_bytes(data)
        item['response_sha256'] = sha(data)
        raise
    item['response_sha256'] = sha(data)
    if fields == FULL:
        filename = symbol + '.csv'
        (folder / filename).write_bytes(data)
        item['raw_file'] = filename
    item.update(inspect_csv(data, expected, fields))
    if item['duplicates'] or item['missing'] or item['extra'] or not item['time_sorted']:
        raise ValueError('minutes failed exact coverage or ordering')
    item['status'] = 'RAW_RANGE_CHECKED' if fields == FULL else 'CLOCK_ONLY_CHECKED'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True)
    p.add_argument('--clock-only', action='store_true', help='request only volume and time, retain counts and hash')
    args = p.parse_args()
    repo = args.repo.resolve()
    original = (repo / 'reports/v711_warmup_receipts.json').read_bytes()
    if sha(original.replace(b'\r\n', b'\n')) != RECEIPT_SHA:
        p.error('expected the published v711_warmup_receipts.json from 1df7180')
    legs = {r['actual_contract']: r for r in json.loads(original)['legs']}
    root = repo / 'data/cache/v711_edb_supplement'
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='clock_' if args.clock_only else 'raw_', dir=root))
    report = dict(study_status='READY_FOR_DATA', historical_sessions='UNVERIFIED_PROXY',
                  purpose='WARMUP_COVERAGE_ONLY', source=EDB, baseline_receipt_lf_sha256=RECEIPT_SHA,
                  expected_source_clock='M1_START_NS', strict_qualified_legs_claimed=0, real_score_runs=0, legs=[])
    for symbol in LEGS:
        item = dict(symbol=symbol, status='FAILED')
        try:
            fetch(symbol, expected_minutes(legs[symbol]), ('volume',) if args.clock_only else FULL, folder, item)
        except (OSError, HTTPException, csv.Error, ValueError, KeyError, ArithmeticError) as exc:
            item['error'] = type(exc).__name__ + ': ' + str(exc)
        report['legs'].append(item)
        (folder / 'receipt.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(symbol, item['status'], 'rows', item.get('rows'), flush=True)
    print(folder / 'receipt.json')
    return int(any(x['status'] == 'FAILED' for x in report['legs']))


if __name__ == '__main__':
    raise SystemExit(main())
