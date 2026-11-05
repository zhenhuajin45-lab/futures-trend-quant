from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import pandas as pd
import pytest
from futures_trend_quant.live import gm_paper_host as h
from futures_trend_quant.live.paper_five import FiveRunner
from test_paper_execution import engine,NOW


class Worker:
    def __init__(self):self.terminated=0
    def is_alive(self):return True
    def terminate(self):self.terminated+=1
    def join(self,timeout):pass


class Runner:
    def __init__(self,tmp_path,readonly=False):
        self.readonly=readonly;self.evidence=tmp_path;self.engine=None;self.run_id='SYNTHETIC';self.cycle=Mock(return_value=False)
        self.event=Mock();self.notice=Mock();self.quote_worker=Worker();self.products={};self.notifications=None
        process=SimpleNamespace(poll=lambda:None,terminate=Mock())
        self.transport=SimpleNamespace(closed=False,process=process)


@pytest.fixture
def setup(tmp_path,monkeypatch):
    r=Runner(tmp_path);factory=Mock(return_value=r)
    monkeypatch.setattr(h,'load_paper_config',lambda path:dict(schema_version=2,config_sha256='TEST'))
    monkeypatch.setattr(h,'FiveRunner',factory);monkeypatch.setattr(h,'timer',Mock(return_value=dict(status=0,timer_id=10001)))
    monkeypatch.setattr(h,'timer_stop',Mock(return_value=True));monkeypatch.setenv('GM_TOKEN','SYNTHETIC_NON_SECRET')
    monkeypatch.delenv('GM_PAPER_HOST_READONLY',raising=False)
    return SimpleNamespace(mode=h.MODE_LIVE),r,factory


def test_official_init_registers_millisecond_timer_and_returns_without_blocking(setup):
    c,r,f=setup;h.init(c)
    h.timer.assert_called_once_with(timer_func=h.on_timer,period=200,start_delay=200)
    assert f.call_args.kwargs==dict(readonly=False) and r.cycle.call_count==0
    assert json.loads((r.evidence/'gm_platform_host.local.json').read_text())['driver']=='GM_INIT_TIMER'
    h.on_timer(c);assert r.cycle.call_count==1
    assert r.last_loop>0 and c.ftq_paper_initialization_seconds>=0


def test_wrong_effective_mode_rejected_before_any_runner_or_trade(setup):
    c,r,f=setup;c.mode=2
    with pytest.raises(ValueError,match='MODE_LIVE'):h.init(c)
    f.assert_not_called();h.timer.assert_not_called()


def test_duplicate_init_and_already_owned_account_never_create_second_controller(setup):
    c,r,f=setup;h.init(c)
    with pytest.raises(ValueError,match='duplicate'):h.init(c)
    assert f.call_count==1
    other=SimpleNamespace(mode=h.MODE_LIVE);f.side_effect=RuntimeError('account mutex already owned')
    with pytest.raises(RuntimeError,match='mutex'):h.init(other)
    assert getattr(other,'ftq_paper_runner',None) is None


def test_missing_token_and_invalid_readonly_flag_fail_closed(setup,monkeypatch):
    c,r,f=setup;monkeypatch.delenv('GM_TOKEN')
    with pytest.raises(ValueError,match='GM_TOKEN'):h.init(c)
    f.assert_not_called();monkeypatch.setenv('GM_TOKEN','SYNTHETIC');monkeypatch.setenv('GM_PAPER_HOST_READONLY','yes')
    with pytest.raises(ValueError,match='exactly'):h.init(c)
    f.assert_not_called()


def test_timer_registration_failure_releases_only_own_workers(setup):
    c,r,f=setup;h.timer.return_value=dict(status=7,timer_id=0)
    with pytest.raises(RuntimeError,match='TIMER'):h.init(c)
    assert r.quote_worker.terminated==1 and r.transport.closed


def test_readonly_completion_stops_timer_without_submitting_orders(setup,monkeypatch):
    c,r,f=setup;monkeypatch.setenv('GM_PAPER_HOST_READONLY','1');r.readonly=True;h.init(c)
    r.cycle.return_value=True;h.on_timer(c)
    assert c.ftq_paper_preflight_passed and f.call_args.kwargs['readonly']
    h.timer_stop.assert_called_with(10001)


def test_reentrant_timer_is_ignored_and_same_cycle_function_reused(setup):
    c,r,f=setup;h.init(c);c.ftq_paper_in_cycle=True;h.on_timer(c);r.cycle.assert_not_called()
    c.ftq_paper_in_cycle=False;h.on_timer(c);assert r.cycle.call_count==1


def test_callback_fault_persists_pause_and_never_claims_flat(setup,tmp_path):
    c,r,f=setup;r.engine,_=engine(tmp_path/'engine');h.init(c);r.cycle.side_effect=OSError('simulated')
    h.on_timer(c)
    saved=json.loads(r.engine.path.read_text());assert saved['pause_new'] and not r.engine.ready and not c.ftq_paper_in_cycle
    assert r.event.call_args.args[0]=='GM_PLATFORM_CYCLE_FAULT'


def test_vendor_error_does_not_record_sensitive_info_or_clear_existing_halt(setup,tmp_path):
    c,r,f=setup;r.engine,_=engine(tmp_path/'engine');r.engine.s['halt_reason']='DAILY_LOSS_LIMIT';h.init(c)
    h.on_error(c,2003,'SECRET_SHOULD_NOT_BE_LOGGED')
    assert r.engine.s['pause_new'] and r.engine.s['halt_reason']=='DAILY_LOSS_LIMIT'
    assert 'SECRET' not in str(r.event.call_args_list)+str(r.notice.call_args_list)


def test_shutdown_is_idempotent_and_does_not_assume_unknown_exposure_is_flat(setup,tmp_path):
    c,r,f=setup;r.engine,_=engine(tmp_path/'engine');r.engine.ready=False;h.init(c);h.on_shutdown(c);h.on_shutdown(c)
    assert r.quote_worker.terminated==1 and r.engine.s['pause_new']
    stop=[x for x in r.event.call_args_list if x.args[0]=='GM_PLATFORM_HOST_STOPPED'][0]
    assert stop.kwargs['flat_confirmed'] is False


def test_real_five_cycle_bounded_readonly_path_has_no_engine_or_order_action(tmp_path):
    from collections import Counter
    import time
    r=FiveRunner.__new__(FiveRunner);r.last_loop=time.monotonic();r.max_gap=0;r.products={};r.quote_worker=Worker();r.engine=None;r.readonly=True
    r.market_events=Mock();r.core_events=Mock();r.transport=SimpleNamespace(drain=lambda:[],busy=lambda:False,request=Mock())
    r.calendar=SimpleNamespace(contracts={});r.last_poll=0;r.last_report=0;r.evidence=tmp_path
    r.report=Mock(return_value=dict(phase='READONLY_PREFLIGHT_PASS'));r.event=Mock()
    assert r.cycle() is True
    r.market_events.assert_called_once();r.core_events.assert_called_once();r.transport.request.assert_not_called()


def test_client_launch_args_take_precedence_and_same_token_reaches_workers(tmp_path,monkeypatch):
    monkeypatch.setenv('GM_TOKEN','OLD_SYNTHETIC');monkeypatch.setenv('GM_STRATEGY_ID','00000000-0000-0000-0000-000000000001')
    sid,token=h.launch_parameters(['--strategy_id','00000000-0000-0000-0000-000000000002','--token','CLIENT_SYNTHETIC','--mode','1'],tmp_path,tmp_path/'absent.json')
    assert sid.endswith('002') and token=='CLIENT_SYNTHETIC'


def test_private_launch_registration_and_auth_do_not_require_global_env_changes(tmp_path,monkeypatch):
    monkeypatch.delenv('GM_TOKEN',raising=False);monkeypatch.delenv('GM_STRATEGY_ID',raising=False)
    p=tmp_path/'registration.json';p.write_text('{"strategy_id":"00000000-0000-0000-0000-000000000003"}')
    (tmp_path/'config').mkdir();(tmp_path/'config/paper_platform_auth.local.json').write_text('{"GM_TOKEN":"PRIVATE_SYNTHETIC"}')
    sid,token=h.launch_parameters([],tmp_path,p)
    assert sid.endswith('003') and token=='PRIVATE_SYNTHETIC' and 'GM_TOKEN' not in __import__('os').environ


def test_unknown_id_or_missing_token_does_not_start_runtime(tmp_path,monkeypatch):
    monkeypatch.delenv('GM_TOKEN',raising=False);monkeypatch.delenv('GM_STRATEGY_ID',raising=False)
    with pytest.raises(ValueError):h.launch_parameters([],tmp_path,tmp_path/'absent')
    with pytest.raises(ValueError,match='GM_TOKEN'):h.launch_parameters(['--strategy_id','00000000-0000-0000-0000-000000000001'],tmp_path,tmp_path/'absent')


def test_git_metadata_failure_keeps_source_hash_provenance_available(monkeypatch,tmp_path):
    import subprocess
    from futures_trend_quant.live.paper_five import code_revision
    monkeypatch.setattr(subprocess,'check_output',Mock(side_effect=subprocess.CalledProcessError(128,'git')))
    assert code_revision(tmp_path)=='UNKNOWN; SEE_SOURCE_HASHES'
