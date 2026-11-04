import ast
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pandas as pd
import pytest
from tools.supplemental_tq.collect import Journal,actual_symbols,prerequisites
from tools.supplemental_tq import collect
from tools.supplemental_tq.status import inspect


def test_explicit_actual_contracts_and_chain(tmp_path):
    assert actual_symbols(["CZCE.SR701","SHFE.au2612","CZCE.SR701"])==["CZCE.SR701","SHFE.au2612"]
    for invalid in ("CZCE.SR2701","SHFE.AU","KQ.m@SHFE.au","DCE.EG2701"):
        with pytest.raises(ValueError):
            actual_symbols([invalid])
    file=tmp_path/"raw.jsonl"
    journal=Journal(file)
    journal.append("TEST",decision_at="UNKNOWN")
    journal.append("END")
    journal.close()
    previous="0"*64
    for line in file.read_text(encoding="utf8").splitlines():
        row=json.loads(line)
        digest=row.pop("sha256")
        assert row["previous_sha256"]==previous
        assert hashlib.sha256(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()==digest
        previous=digest


def test_no_order_or_real_account_methods():
    tree=ast.parse(Path(__file__).with_name("collect.py").read_text(encoding="utf8"))
    forbidden={"insert_order","cancel_order","get_account","get_position","TqAccount","TargetPosTask"}
    called={node.func.attr if isinstance(node.func,ast.Attribute) else node.func.id
            for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,(ast.Attribute,ast.Name))}
    assert not called&forbidden


def test_market_update_loop_snapshot_rollover_and_final_close(tmp_path,monkeypatch):
    clock=iter(range(100))
    monkeypatch.setattr(collect.time,"monotonic",lambda:next(clock))
    monkeypatch.setattr(collect,"authentication",lambda:("offline-user","offline-password"))
    instances=[]
    class FakeApi:
        def __init__(self,**kwargs):
            self.frames=[]
            self.closed=False
            instances.append(self)
        def get_quote(self,symbol):
            return SimpleNamespace(datetime="2026-11-04 09:30:00",last_price=100.)
        def get_kline_serial(self,symbol,duration_seconds,data_length):
            assert duration_seconds==60
            frame=pd.DataFrame(dict(id=[1,2,3],datetime=[1780000020000000000,1780000080000000000,1780000140000000000],
                open=[100.,101.,102.],high=[101.,102.,103.],low=[99.,100.,101.],close=[100.,101.,102.],volume=[10,10,10],open_oi=[100,100,100],close_oi=[100,100,100]))
            self.frames.append(frame)
            return frame
        def wait_update(self,deadline):
            for frame in self.frames:
                frame["datetime"]+=60000000000
            return True
        def is_changing(self,obj):
            return True
        def is_serial_ready(self,obj):
            return True
        def close(self):
            self.closed=True
    monkeypatch.setitem(sys.modules,"tqsdk",SimpleNamespace(TqApi=FakeApi,TqAuth=lambda *args:None,TqSim=lambda:None,__version__="offline-fixture"))
    out=tmp_path/"trial"
    collect.run(["SHFE.au2612","DCE.eg2701"],out,12)
    result=inspect(out/"raw.jsonl")
    assert result["chain_valid"] and result["ended"] and not result["errors"]
    assert result["contracts_with_observed_closed_m1"]==2
    assert instances[0].closed
    assert not result["full_session_coverage_verified"]
