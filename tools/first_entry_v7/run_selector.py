"""Compatibility entry; V7.1 independent-calendar admission is mandatory."""
from pathlib import Path
import sys
import pandas as pd
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def complete_past_ends(actual_eobs,expected_eobs):
    """Legacy utility only: 1 M1 can never masquerade as a full M15.

    Admission additionally requires an independent calendar, in first_entry_v71.
    """
    if len(actual_eobs)!=len(set(actual_eobs)):
        raise ValueError('duplicate raw minute')
    actual=set(actual_eobs)
    expected={}
    for time in expected_eobs:
        expected.setdefault(time.ceil('15min'),set()).add(time)
    return sorted(end for end,times in expected.items()
                  if len(times)==15 and times<=actual and max(times)==end)


def run(manifest_path,input_root,output,sample_lock=None,fixture=False,score=False):
    from tools.first_entry_v71.entrypoint import run as checked_run
    from tools.first_entry_v71.admission import DEFAULT_LOCK
    return checked_run(manifest_path,input_root,output,DEFAULT_LOCK if sample_lock is None else sample_lock,fixture,score)


if __name__=='__main__':
    from tools.first_entry_v71.entrypoint import main
    main()
