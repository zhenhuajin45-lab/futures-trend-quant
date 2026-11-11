"""Consumed TQ inputs survive restart and cannot be silently revised."""
from datetime import datetime,timezone,timedelta
from dataclasses import asdict,replace
from pathlib import Path
import json
import pytest
from futures_trend_quant.core.models import Bar
from futures_trend_quant.live.paper_data import SignalWorker,restore_tq_inputs
from futures_trend_quant.live.paper_tq import POLICY,append
from futures_trend_quant.live.paper_tq_market import merge_same_source
from futures_trend_quant.live.paper_calendar import PaperCalendar


def fixture(tmp_path):
    c=dict(root=str(tmp_path),state_dir=str(tmp_path/'state'),external_market_policy=POLICY,csymbol='CZCE.MA',config_sha256='SYNTHETIC')
    start=datetime(2026,11,11,9,tzinfo=timezone(timedelta(hours=8)))
    bars=[Bar('CZCE.MA701','60s',start+timedelta(minutes=i),start+timedelta(minutes=i+1),100,102,99,101,volume=10,position=100) for i in range(3)]
    w=SignalWorker(c,child=True);w.seed_cache=dict(path='IMMUTABLE_INITIAL_SEED',sha256='INITIAL',symbol='CZCE.MA701',cutoff=bars[0].eob.isoformat())
    w.last=bars[0].eob;return c,bars,w


def test_actual_input_journal_checkpoint_restore_bridges_beyond_initial_seed(tmp_path):
    c,bars,w=fixture(tmp_path)
    w.runtime.used_evidence_ids.add('ALREADY_CONSUMED')
    w._record_tq_inputs([bars[1]],{bars[0].identity:bars[0]},dict(returned_at=bars[1].eob.isoformat()))
    w.last=bars[1].eob;w._checkpoint()
    saved=json.loads(w.checkpoint.read_text());restored=SignalWorker(c,child=True)
    observed=restore_tq_inputs(c,[bars[0]],restored.last,restored.tq_input_proof)
    assert observed==bars[:2] and restored.runtime.to_dict()==w.runtime.to_dict()
    assert restored.seed_cache==w.seed_cache and saved['tq_input_prefix']['bytes']>0
    cal=PaperCalendar(['2026-11-10','2026-11-11','2026-11-12'],[dict(start='09:00',end='10:15')],'CZCE.MA')
    with pytest.raises(ValueError,match='GAP'):merge_same_source([bars[0]],[bars[2]],cal)
    merged,added=merge_same_source(observed,[bars[2]],cal)
    assert merged==bars and added==[bars[2]]


def test_consumed_journal_prefix_change_and_wrong_path_are_rejected(tmp_path):
    c,bars,w=fixture(tmp_path);w._record_tq_inputs([bars[1]],{bars[0].identity:bars[0]},{});w.last=bars[1].eob;w._checkpoint()
    saved=json.loads(w.checkpoint.read_text());proof=saved['tq_input_prefix']
    with pytest.raises(ValueError,match='PATH_MISMATCH'):restore_tq_inputs(c,bars[:1],w.last,dict(proof,path=str(tmp_path/'other')))
    p=Path(proof['path']);p.write_bytes(p.read_bytes().replace(b'101',b'100',1))
    with pytest.raises(ValueError,match='PREFIX_CHANGED'):restore_tq_inputs(c,bars[:1],w.last,proof)


def test_committed_tail_is_preserved_but_revised_closed_values_never_fallback(tmp_path):
    c,bars,w=fixture(tmp_path);w._record_tq_inputs([bars[1]],{bars[0].identity:bars[0]},{});w.last=bars[1].eob;w._checkpoint()
    saved=json.loads(w.checkpoint.read_text());proof=saved['tq_input_prefix']
    w._record_tq_inputs([bars[2]],{b.identity:b for b in bars[:2]},dict(returned_at=bars[2].eob.isoformat()))
    assert restore_tq_inputs(c,bars[:1],w.last,proof)==bars
    raw=asdict(replace(bars[1],close=100));raw.update(bob=bars[1].bob.isoformat(),eob=bars[1].eob.isoformat())
    append(Path(c['state_dir'])/'tq_core_m1.jsonl',dict(policy=POLICY,csymbol=c['csymbol'],bar=raw))
    with pytest.raises(ValueError,match='JOURNAL_REVISION'):restore_tq_inputs(c,bars[:1],w.last,proof)


def test_missing_consumed_interval_cannot_be_hidden_by_old_initial_cache(tmp_path):
    c,bars,w=fixture(tmp_path)
    with pytest.raises(ValueError,match='USED_INPUT_PREFIX_MISSING'):restore_tq_inputs(c,bars[:1],bars[1].eob)


def test_gm_products_do_not_acquire_tq_input_journals(tmp_path):
    c,bars,w=fixture(tmp_path);w.c['csymbol']='DCE.M'
    w._record_tq_inputs([bars[1]],{bars[0].identity:bars[0]},{});w._checkpoint()
    assert not (Path(c['state_dir'])/'tq_core_m1.jsonl').exists()
    assert 'tq_input_prefix' not in json.loads(w.checkpoint.read_text())
