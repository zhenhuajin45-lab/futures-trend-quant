"""Concurrent publication must keep complete fresh names; no old-proof fallback."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
from futures_trend_quant.live.paper_tq import atomic,append,latest_committed
from futures_trend_quant.live.paper_io import read_json
from futures_trend_quant.live.paper_tq_market import OwnedFeed


def test_shared_quote_progress_atomic_replacement_under_concurrent_readers(tmp_path):
    p=tmp_path/'health.jsonl';append(p,dict(sequence=0,payload='0'*8192))
    done=threading.Event();counts=[]
    def reader():
        count=0
        while not done.is_set():
            r=latest_committed(p);assert r['payload']==str(r['sequence']%10)*8192;count+=1
        counts.append(count)
    with ThreadPoolExecutor(max_workers=6) as pool:
        readers=[pool.submit(reader) for _ in range(5)]
        try:
            for i in range(1,1501):append(p,dict(sequence=i,payload=str(i%10)*8192))
        finally:done.set()
        for future in readers:future.result()
    assert len(counts)==5 and all(n>0 for n in counts)
    assert latest_committed(p)['sequence']==1500


def test_owned_shutdown_requests_matching_generation_without_terminating_a_launcher_only(tmp_path):
    feed=OwnedFeed(dict(root=str(tmp_path),evidence_dir=str(tmp_path/'ev'),config_sha256='C'),'CONFIG','GEN')
    feed.spool.mkdir(parents=True);feed.process=Mock()
    feed.process.poll.return_value=None;feed.process.wait.return_value=0
    feed.terminate()
    assert read_json(feed.spool/'stop.request.json')==dict(run_id='GEN',config_sha256='C')
    feed.process.terminate.assert_not_called()
    feed.process.wait.assert_called_once_with(timeout=3)


def test_busy_source_publication_never_refreshes_old_proof(tmp_path,monkeypatch):
    import futures_trend_quant.live.paper_tq as tq
    p=tmp_path/'proof.json';atomic(p,dict(sequence=1))
    exc=PermissionError('SYNTHETIC');exc.winerror=32
    monkeypatch.setattr(tq.os,'replace',Mock(side_effect=exc))
    with pytest.raises(PermissionError):atomic(p,dict(sequence=2))
    assert read_json(p)['sequence']==1
    assert tq.os.replace.call_count==3


def test_only_complete_journal_records_are_progress_and_parse_failures_do_not_fallback(tmp_path):
    p=tmp_path/'health.jsonl';append(p,dict(sequence=1,updated_at='ORIGINAL'))
    with p.open('ab') as f:f.write(b'{"sequence":2')
    assert latest_committed(p)==dict(sequence=1,updated_at='ORIGINAL')
    with p.open('ab') as f:f.write(b'INVALID}\n')
    with pytest.raises(ValueError):latest_committed(p)
