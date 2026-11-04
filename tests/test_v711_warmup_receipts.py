"""Raw provenance/time contracts; synthetic only, no provider requests."""
import pandas as pd
import pytest
from backtest.forward_v3 import digest
from tools.first_entry_v71.warmup_receipts import merge_sources,intervals


def source(folder,name,close=100.):
    bob=pd.Timestamp('2025-11-05T09:00:00+08:00')
    frame=pd.DataFrame(dict(datetime_nano=[bob.value,(bob+pd.Timedelta(minutes=1)).value],
        open=100.,high=max(100.,close),low=100.,close=close,volume=[0,3],open_oi=10,close_oi=11))
    path=folder/name;frame.to_csv(path,index=False)
    return dict(symbol='DCE.jd2605',response_file=name,response_sha256=digest(path))


def test_identical_overlap_preserves_zero_volume_and_start_end_clock(tmp_path):
    a=source(tmp_path,'a.csv');b=source(tmp_path,'b.csv')
    frame,hashes,duplicates=merge_sources([a,b],tmp_path,'DCE.jd2605')
    assert duplicates==2 and len(frame)==2 and len(hashes)==2
    assert frame.iloc[0].bob==pd.Timestamp('2025-11-05T09:00:00+08:00')
    assert frame.iloc[0].eob==pd.Timestamp('2025-11-05T09:01:00+08:00')
    assert frame.iloc[0].volume==0 and frame.iloc[0].close_oi==11


def test_conflicting_overlap_is_not_silently_deduplicated(tmp_path):
    a=source(tmp_path,'a.csv');b=source(tmp_path,'b.csv',101.)
    with pytest.raises(ValueError,match='conflicting duplicate minute'):
        merge_sources([a,b],tmp_path,'DCE.jd2605')


def test_response_changed_after_receipt_fails_before_parsing(tmp_path):
    a=source(tmp_path,'a.csv');(tmp_path/'a.csv').write_text('replaced',encoding='utf8')
    with pytest.raises(ValueError,match='raw response changed'):
        merge_sources([a],tmp_path,'DCE.jd2605')


def test_missing_intervals_keep_session_break_separate():
    rows=intervals({pd.Timestamp('2025-11-05T10:15:00+08:00'),
        pd.Timestamp('2025-11-05T10:31:00+08:00'),pd.Timestamp('2025-11-05T10:32:00+08:00')})
    assert [r['minutes'] for r in rows]==[1,2]
    assert rows[0]['last_eob']=='2025-11-05T10:15:00+08:00'
    assert rows[1]['first_eob']=='2025-11-05T10:31:00+08:00'
