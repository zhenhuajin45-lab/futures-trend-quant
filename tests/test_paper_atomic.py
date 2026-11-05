import json
import pytest
from futures_trend_quant.live.paper_broker import atomic
from futures_trend_quant.live.paper_notify import atomic_json


@pytest.mark.parametrize('write',[atomic,atomic_json])
def test_transient_windows_reader_lock_retries_bounded_without_dropping_state(tmp_path,monkeypatch,write):
    import os
    real=os.replace;calls=[]
    def replace(a,b):
        calls.append(1)
        if len(calls)<3:
            exc=PermissionError('reader sharing lock');exc.winerror=5;raise exc
        return real(a,b)
    monkeypatch.setattr(os,'replace',replace)
    p=tmp_path/'execution.json';write(p,dict(state='PRESERVED'))
    assert len(calls)==3 and json.loads(p.read_text())['state']=='PRESERVED'


@pytest.mark.parametrize('write',[atomic,atomic_json])
def test_permanent_write_failure_is_not_swallowed_and_prior_state_survives(tmp_path,monkeypatch,write):
    import os
    p=tmp_path/'execution.json';p.write_text('{"state":"OLD"}');calls=[]
    def fail(a,b):
        calls.append(1);exc=PermissionError('permanent');exc.winerror=5;raise exc
    monkeypatch.setattr(os,'replace',fail)
    with pytest.raises(PermissionError):write(p,dict(state='NEW'))
    assert len(calls)==5 and json.loads(p.read_text())['state']=='OLD'
    assert len(list(tmp_path.iterdir()))==1
