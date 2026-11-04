"""Freeze independent metadata and file hashes BEFORE replay selection/outcomes."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,digest
from tools.historical_strategy.reproduce import canonical_digest
from tools.first_entry_v71.clock import make_lock,AdmissionError


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--calendar',type=Path,required=True)
    p.add_argument('--input-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.output.exists():raise AdmissionError('SAMPLE_LOCK_ALREADY_EXISTS')
    bundle=read(a.calendar)
    if bundle.get('purpose')!='INDEPENDENT_RESEARCH':raise AdmissionError('REAL_METADATA_FREEZE_ONLY')
    for relative,expected in bundle['input_file_sha256'].items():
        path=(a.input_root/relative).resolve()
        if not path.is_relative_to(a.input_root.resolve()) or digest(path)!=expected:
            raise AdmissionError('INPUT_CHANGED_DURING_FREEZE',file=relative)
    for proof in bundle['evidence_files']:
        path=(a.input_root/proof['path']).resolve()
        if not path.is_relative_to(a.input_root.resolve()) or digest(path)!=proof['sha256']:
            raise AdmissionError('CALENDAR_PROOF_CHANGED')
    registration=read(ROOT/'tools/first_entry_v7/registration.json')
    lock=make_lock(bundle,registration,canonical_digest(ROOT/'tools/first_entry_v7/registration.json'),digest(a.calendar))
    a.output.parent.mkdir(parents=True,exist_ok=True);write(a.output,lock)
    print('SAMPLE_METADATA_AND_HASHES_FROZEN_NO_LABELS_READ',flush=True)


if __name__=='__main__':main()
