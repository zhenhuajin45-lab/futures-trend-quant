import importlib.util
from pathlib import Path
import pandas as pd

s=importlib.util.spec_from_file_location("edb",Path(__file__).with_name("edb_compare.py"))
edb=importlib.util.module_from_spec(s)
s.loader.exec_module(edb)


def test_edb_start_nanos_become_close_and_differences_use_tick_units():
    start=pd.Timestamp("2026-08-14T09:00:00+08:00")
    b=edb.normalize_edb(pd.DataFrame([dict(datetime_nano=start.value,open=100.,high=105.,low=95.,
                                          close=100.,volume=4,open_oi=10,close_oi=12)]))
    assert b.eob.iloc[0]==start+pd.Timedelta(minutes=1)
    gm=pd.DataFrame([dict(eob=b.eob.iloc[0],open=105.,high=110.,low=100.,close=105.,volume=8,position=12)])
    r=edb.compare(gm,b,5).iloc[0]
    assert r.close_diff_ticks==1 and r.volume_diff==4 and r.end_oi_diff==0 and r.missing==""
