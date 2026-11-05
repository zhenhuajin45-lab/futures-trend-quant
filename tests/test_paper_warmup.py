import hashlib,json
import pandas as pd
import pytest
from futures_trend_quant.live.paper_warmup import normalize_no_trade_minutes


def fixture(tmp_path,change=None):
    cache=tmp_path/'data/cache';cache.mkdir(parents=True)
    times=pd.date_range('2026-10-01T09:01:00+08:00',periods=6,freq='min')
    target=times[2]
    rows=[dict(symbol='DCE.jd2612',frequency='60s',bob=t-pd.Timedelta(minutes=1),eob=t,open=100.,high=100.,low=100.,close=100.,volume=10.,amount=100.,position=20) for t in times if t!=target]
    frame=pd.DataFrame(rows)
    evidence=pd.DataFrame([dict(datetime_nano=(t-pd.Timedelta(minutes=1)).value,open=100.,high=100.,low=100.,close=100.,volume=0. if t==target else 10.,open_oi=20,close_oi=20) for t in times])
    if change:change(evidence,frame)
    csv=cache/'original.csv';evidence.to_csv(csv,index=False)
    proof=cache/'manifest.json';proof.write_text(json.dumps(dict(purpose='VERIFIED_ZERO_TRADE_WARMUP_ONLY',entries=[dict(symbol='DCE.jd2612',eob=target.isoformat(),csv='data/cache/original.csv',sha256=hashlib.sha256(csv.read_bytes()).hexdigest())])))
    return frame,proof,target


def test_only_explicit_zero_trade_warmup_added_and_original_unmodified(tmp_path):
    frame,proof,t=fixture(tmp_path);before=frame.copy(deep=True)
    result,receipts=normalize_no_trade_minutes(frame,proof,tmp_path,'DCE.jd2612',t+pd.Timedelta(minutes=3))
    assert len(result)==6 and len(receipts)==1
    assert result.loc[result.eob==t].iloc[0].volume==0
    assert receipts[0]['historical_arrival_clock']=='UNKNOWN'
    pd.testing.assert_frame_equal(frame,before)


@pytest.mark.parametrize('column,value',[('volume',1),('open',101),('close',float('nan')),('open_oi',21),('close_oi',21)])
def test_unknown_trading_or_changed_target_is_never_filled(tmp_path,column,value):
    frame,proof,t=fixture(tmp_path,lambda e,f:e.loc.__setitem__((2,column),value))
    with pytest.raises(ValueError):normalize_no_trade_minutes(frame,proof,tmp_path,'DCE.jd2612',t+pd.Timedelta(minutes=3))


def test_tampered_original_rejected(tmp_path):
    frame,proof,t=fixture(tmp_path);(tmp_path/'data/cache/original.csv').write_text('tampered')
    with pytest.raises(ValueError,match='hash'):normalize_no_trade_minutes(frame,proof,tmp_path,'DCE.jd2612',t)


def test_no_future_filling_or_overwriting_existing_gm_bar(tmp_path):
    frame,proof,t=fixture(tmp_path)
    result,r=normalize_no_trade_minutes(frame,proof,tmp_path,'DCE.jd2612',t-pd.Timedelta(minutes=1))
    assert not r and len(result)==5
    frame=pd.concat([frame,pd.DataFrame([dict(frame.iloc[0],eob=t,close=111.)])],ignore_index=True)
    result,r=normalize_no_trade_minutes(frame,proof,tmp_path,'DCE.jd2612',t)
    assert not r and result.loc[result.eob==t].iloc[0].close==111


def test_unexplained_neighbor_volume_not_accepted(tmp_path):
    frame,proof,t=fixture(tmp_path,lambda e,f:e.loc.__setitem__((0,'volume'),11))
    with pytest.raises(ValueError,match='volume'):normalize_no_trade_minutes(frame,proof,tmp_path,'DCE.jd2612',t)


def test_one_contract_adjacent_boundary_shift_is_recorded_without_overwrite(tmp_path):
    def change(e,f):e.loc[3,'volume']=9;e.loc[4,'volume']=11
    frame,proof,t=fixture(tmp_path,change)
    result,r=normalize_no_trade_minutes(frame,proof,tmp_path,'DCE.jd2612',t)
    assert r[0]['neighbor_volume_boundary_differences']==2
    assert result.loc[result.eob!=t].volume.eq(10).all()
