"""The three review counterexamples, with no historical market observations."""
import gzip
import json
import pandas as pd
import pytest
from pathlib import Path
from backtest.forward_v3 import read, digest
from tools.historical_strategy.reproduce import canonical_digest
from tools.first_entry_v7.source_qualification import require_qualified
from tools.first_entry_v7.run_selector import complete_past_ends, run

ROOT = Path(__file__).resolve().parents[1]


def test_reject_manifest_shortened_to_march():
    registration = read(ROOT/'tools/first_entry_v7/registration.json')
    product = dict(calendar_evidence_sha256='x', schedule_sha256='x', selection_sha256='x',
                   contracts=[dict(past_complete_m15=1200, raw_sha256='x')])
    manifest = dict(status='QUALIFIED_BEFORE_SCORING', registered_before_labels=True,
        first_scoring_eob='2026-03-02T09:15:00+08:00', last_scoring_eob='2026-03-03T15:00:00+08:00',
        last_path_eob='2026-05-06T15:00:00+08:00', products={p: product for p in registration['new_sample']['universe']})
    with pytest.raises(ValueError):
        require_qualified(manifest, registration)


def test_single_minute_cannot_be_complete_m15():
    fake = list(pd.date_range('2025-11-01T09:15:00+08:00', periods=1200, freq='15min'))
    assert len(complete_past_ends(fake, fake)) == 0


def test_header_only_cannot_write_success_receipt(tmp_path):
    registration = read(ROOT/'tools/first_entry_v7/registration.json')
    products = {}
    schedule = [dict(eob='2026-01-05T09:15:00+08:00', trading_date='2026-01-05')]
    for i, csymbol in enumerate(registration['new_sample']['universe']):
        symbol = csymbol+'.fixture'
        raw = tmp_path/f'raw{i}.parquet'
        pd.DataFrame({'eob': [pd.Timestamp(schedule[0]['eob'])]}).to_parquet(raw)
        paths = {}
        for name, value in (('schedule', schedule), ('selection', {'2026-01-05':symbol}), ('calendar_evidence', {})):
            path=tmp_path/f'{name}{i}.json';path.write_text(json.dumps(value), encoding='utf8')
            paths[name+'_path']=path.name;paths[name+'_sha256']=digest(path)
        replay=tmp_path/f'replay{i}.gz'
        with gzip.open(replay,'wt',encoding='utf8') as stream:
            stream.write(json.dumps(dict(task=dict(csymbol=csymbol,symbol=symbol),input_sha256=digest(raw)))+'\n')
        products[csymbol]=dict(paths,contracts=[dict(symbol=symbol,past_complete_m15=1200,
            raw_path=raw.name,raw_sha256=digest(raw),replay_path=replay.name,replay_sha256=digest(replay))])
    manifest=dict(status='QUALIFIED_BEFORE_SCORING',registered_before_labels=True,
        first_scoring_eob='2026-01-05T09:15:00+08:00',last_scoring_eob='2026-04-24T15:00:00+08:00',
        last_path_eob='2026-05-06T15:00:00+08:00', products=products,
        registration_sha256=canonical_digest(ROOT/'tools/first_entry_v7/registration.json'))
    path=tmp_path/'manifest.json';path.write_text(json.dumps(manifest),encoding='utf8')
    with pytest.raises(ValueError):
        run(path,tmp_path,tmp_path/'out')
    assert not (tmp_path/'out/receipt.json').exists()
