from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
import ast,json
import pytest
from futures_trend_quant.live.trend_observation import build,lines,safe_from_result
from research.futures_trend_observer.observer import FuturesTrendObserver

T=datetime(2026,11,6,10,tzinfo=timezone(timedelta(hours=8)))


def facts(t=T):
    return dict(symbol='SHFE.au2612',eob=t.isoformat(),bar_id='SHFE.au2612|900s|'+t.isoformat(),
        h1_absolute='bull_strong',slope_state='up',trend_inputs=dict(close_side='above'),
        top_div=True,bottom_div=False,trade_permission=dict(policy='CLOSED_FACTS_PERMISSION_V1',background='bull',
        reasons=['M15_OPPOSITE_DIVERGENCE_ACTIVE'],new_entry_allowed=False,weak_closed_bars=0),
        top_divergence_event=dict(kind='top',mode='B_hist',previous_bar_id='SHFE.au2612|900s|2026-11-04T09:15:00+08:00',
            current_bar_id='SHFE.au2612|900s|2026-11-05T10:15:00+08:00',defined_at_bar_id='SHFE.au2612|900s|2026-11-05T11:00:00+08:00',extreme_price=890.88))


def context():return dict(background='bull',hist=.1,previous_hist=.2,closed=True,complete=True,episode_start='2026-11-05T21:15:00+08:00',computed_at=T.isoformat())


def test_background_alignment_warning_and_permission_are_four_distinct_values():
    o=build(facts(),**context())
    assert o['background_direction']=='bull' and o['local_direction'] is None
    assert o['local_direction_basis']=='UNKNOWN_NO_INDEPENDENT_LOCAL_STRUCTURE'
    assert o['momentum_state']=='POSITIVE_CONTRACTING' and o['phase']=='ALIGNED'
    assert o['warning_active'] is None and o['entry_veto_active'] is True
    assert o['observer_eligible'] is None and o['production_permission']['new_entry_allowed'] is False
    assert o['divergence_events'][0]['confirmed_before_current_episode']
    assert '890.88' in ' '.join(lines(o)) and '无固定到期' in ' '.join(lines(o))


@pytest.mark.parametrize('change,expected',[
 ({'complete':False},'UNKNOWN'),({'closed':False},'UNKNOWN'),({'hist':None},'UNKNOWN'),
 ({'background':'none'},'UNESTABLISHED'),({'hist':-.2},'MIXED')])
def test_phase_priority_does_not_invent_ranging_or_opposite_trend(change,expected):
    c=context();c.update(change);o=build(facts(),**c)
    assert o['phase']==expected and o['local_direction'] is None
    assert o['range_id'] is None and o['box_atr'] is None


def test_existing_weakness_and_exit_have_priority_over_alignment():
    f=facts();f['trade_permission']['weak_closed_bars']=1
    assert build(f,**context())['phase']=='WEAKENING'
    f['trade_permission']['technical_exit_direction']='bull'
    assert build(f,**context())['phase']=='TECHNICAL_EXIT_PENDING'
    f['trade_permission']['weak_closed_bars']=0;f['trade_permission'].pop('technical_exit_direction')
    f['trend_decision']=dict(proposed_action='veto')
    assert build(f,**context())['phase']=='WEAKENING'


def test_future_confirmation_refused_and_observation_error_isolated():
    f=facts();f['top_divergence_event']['defined_at_bar_id']='SHFE.au2612|900s|2026-11-06T10:15:00+08:00'
    with pytest.raises(ValueError):build(f,**context())
    assert safe_from_result(object())['data_quality']=='OBSERVATION_ERROR'
    assert lines(dict(divergence_events=[{}]))==['状态说明资料异常：UNKNOWN；不改变生产权限及风险退出。']


def test_same_input_restart_and_future_independence():
    observer=FuturesTrendObserver('SHFE.au2612');first=observer.push(facts(),asof=T,expected_previous_eob=None,**context())
    raw=json.loads(json.dumps(observer.to_dict()));restored=FuturesTrendObserver.from_dict(raw)
    f=facts(T+timedelta(minutes=15));c=context();c['hist']=-1
    restored.push(f,asof=T+timedelta(minutes=15),expected_previous_eob=T.isoformat(),**c)
    assert restored.records[T.isoformat()]['observation']==first
    assert observer.to_dict()==raw
    continuous=observer.push(f,asof=T+timedelta(minutes=15),expected_previous_eob=T.isoformat(),**c)
    assert continuous==restored.records[f['eob']]['observation']


@pytest.mark.parametrize('kind',['out_of_order','revision','contract','gap','future'])
def test_bad_sequence_keeps_original_prefix(kind):
    o=FuturesTrendObserver('SHFE.au2612');o.push(facts(),asof=T,expected_previous_eob=None,**context());before=o.to_dict()
    f=facts(T+timedelta(minutes=15));previous=T.isoformat();asof=T+timedelta(minutes=15)
    if kind=='out_of_order':f=facts(T-timedelta(minutes=15))
    if kind=='revision':f=facts();f['slope_state']='down'
    if kind=='contract':f['symbol']='SHFE.au2702'
    if kind=='gap':previous=None
    if kind=='future':asof=T
    with pytest.raises(ValueError):o.push(f,asof=asof,expected_previous_eob=previous,**context())
    assert before==o.to_dict()


def test_missing_and_forming_bars_stay_unknown_without_advancing_sequence():
    o=FuturesTrendObserver('SHFE.au2612');c=context();c['complete']=False
    assert o.push(facts(),asof=T,expected_previous_eob=None,**c)['phase']=='UNKNOWN'
    assert o.last_eob is None


def test_no_new_trading_or_ea_dependencies():
    root=Path(__file__).resolve().parents[1]
    for p in [root/'research/futures_trend_observer/observer.py',root/'tools/futures_trend_review.py',root/'futures_trend_quant/live/trend_observation.py']:
        tree=ast.parse(p.read_text(encoding='utf8'))
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):assert not any(x in (node.module or '').lower() for x in ('broker','gmtrade','paper_data','mt5','233'))
            if isinstance(node,ast.Import):assert all(x.name not in ('gm','gmtrade','MetaTrader5') for x in node.names)


def test_message_path_reuses_actual_observation_and_does_not_authorize():
    from futures_trend_quant.live.paper_explain import lines as reason_lines
    from futures_trend_quant.live.paper_cards import candidate_card
    o=build(facts(),**context());c=dict(trend_observation=o,permission=o['production_permission'])
    assert '正在否决' in ' '.join(reason_lines(c))
    plan=dict(symbol='SHFE.au2612',direction='bull',first_eligible_at=T.isoformat(),expires_at=(T+timedelta(seconds=30)).isoformat(),
        structural_valid=True,quote_valid=True,entry=886.4,stop=882.,target=895.2,max_age_seconds=30,open_wait_seconds=10,
        trade_reason=c,path='B')
    original=deepcopy(plan);card=candidate_card(plan,(False,'CANDIDATE_WAITING'),T,'黄金')
    text=json.dumps(card,ensure_ascii=False)
    assert '890.88' in text and '正在否决' in text and '882.00' in text and '895.20' in text
    assert plan==original


def test_observation_does_not_share_mutable_permission_with_production():
    f=facts();o=build(f,**context());o['production_permission']['reasons'].clear()
    assert f['trade_permission']['reasons']==['M15_OPPOSITE_DIVERGENCE_ACTIVE']


def test_five_product_two_events_summary_payload_and_original_clocks(tmp_path,monkeypatch):
    import tools.paper_trend_notify as notify
    root=tmp_path/'_strategy_runtime';(root/'config').mkdir(parents=True)
    (root/'config/paper_feishu.local.json').write_text(json.dumps(dict(enabled=False)),encoding='utf8')
    (root/'evidence/paper_observation_five').mkdir(parents=True)
    f=facts();f['bottom_div']=True
    f['bottom_divergence_event']={**f['top_divergence_event'],'kind':'bottom','extreme_price':831.0}
    o=build(f,**context());items=[]
    for cs,name in notify.POOL:
        items.append(dict(csymbol=cs,name=name,symbol='SHFE.au2612',direction='bull',active=True,state_fresh=True,
            reason='尚未报单',leave_votes=0,blocked_dir=None,evidence=f,previous_evidence={},trend_observation=o))
    monkeypatch.setattr(notify,'rows',lambda *a: (dict(at=T.isoformat()),items))
    key=notify.deliver(tmp_path,'PAYLOAD','15分钟·五品种趋势判断',T)
    record=json.loads((root/'evidence/paper_observation_five/notifications'/f'{key}.json').read_text(encoding='utf8'))
    assert record['status']=='LOCAL_ONLY_NOT_CONFIGURED'
    assert len(json.dumps(record['text'],ensure_ascii=False,separators=(',',':')).encode())<19000
    assert record['text']['card']['header']['title']['content'].count('截至 ')==1


def test_registered_cross_zero_examples_use_actual_production_function():
    from futures_trend_quant.core.facts.indicators import IndicatorSet
    from futures_trend_quant.core.facts.divergence import _b_hist_channels
    from futures_trend_quant.core.models import Pivot
    root=Path(__file__).resolve().parents[1]
    cases=json.loads((root/'research/futures_trend_observer/hist_convention_cases.json').read_text(encoding='utf8'))['cases']
    for case in cases:
        kind=case['kind'];difs=(.8,.79) if kind=='top' else (-.8,-.79)
        indicators=IndicatorSet((),(),difs,(),(case['previous_hist'],case['current_hist']),(),(),())
        pk='high' if kind=='top' else 'low'
        channels=_b_hist_channels(kind,Pivot(0,0,pk,100,1),Pivot(1,1,pk,101,1),indicators,.05,.05,.12)
        assert ('hist' in channels)==case['production_hist_channel']
        assert 'dif' not in channels


def test_explanation_release_registers_only_display_metadata(tmp_path,monkeypatch):
    from tools import paper_postclose_v3_release as release
    from types import SimpleNamespace
    client=tmp_path/'active';bundle=tmp_path/'release';stage=bundle/'client/_strategy_runtime/config';stage.mkdir(parents=True)
    profile=stage/'paper_observation_five.local.json'
    original=dict(risk_pct=.0025,daily_loss_pct=.01,observation_drawdown_pct=.05,margin_usage_pct=.2,permission_policy='V1',
                  execution_policy_version='POSTCLOSE_EXEC_V3_20261106',notification_policy_version='ACTIONABLE_CARDS_V4_20261106')
    profile.write_text(json.dumps(original),encoding='utf8');(bundle/'release_plan.local.json').write_text('{}',encoding='utf8')
    monkeypatch.setattr(release.prior,'prepare',lambda *a:None)
    monkeypatch.setattr(release.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='{}'))
    release.prepare(client,bundle,trend_explanation_v1=True)
    result=json.loads(profile.read_text(encoding='utf8'))
    assert result.pop('trend_observation_version')=='S0_TREND_OBSERVATION_V1'
    assert result==original


def test_derived_csv_keeps_nested_source_datetimes_and_response_missing_unknown(tmp_path):
    from tools.futures_trend_review import write_csv,response
    target=tmp_path/'source.csv';write_csv(target,[dict(h1_source=dict(known_at=T))])
    assert T.isoformat(sep=' ') in target.read_text(encoding='utf-8-sig')
    assert response({},[T+timedelta(minutes=1)],T,'bull',880,15)['status']=='UNKNOWN_MISSING_OR_UNFINISHED'


def test_account_context_never_invokes_observer_even_if_it_would_stall(monkeypatch):
    from futures_trend_quant.live import trend_observation,paper_explain
    monkeypatch.setattr(trend_observation,'safe_from_result',lambda _: (_ for _ in ()).throw(AssertionError('observer ran in risk process')))
    result=SimpleNamespace(analysis=SimpleNamespace(finalized_h1_absolute='bull',finalized_h1_eob=T,bar=SimpleNamespace(eob=T)),
        entry=None,trend=SimpleNamespace(confirmed_state='bull'),facts_row=dict(slope_state='up'),gate=None)
    original=dict(phase='ALIGNED',production_permission=dict(new_entry_allowed=False))
    assert paper_explain.context(result,original)['trend_observation']==original
    assert paper_explain.context(result)['trend_observation'] is None


def test_reprocessing_same_closed_facts_does_not_rewrite_original_computation_clock():
    o=FuturesTrendObserver('SHFE.au2612');first=o.push(facts(),asof=T,expected_previous_eob=None,**context())
    c=context();c['computed_at']=(T+timedelta(hours=1)).isoformat()
    assert o.push(facts(),asof=T+timedelta(hours=1),expected_previous_eob=None,**c)==first


def test_signal_child_hands_observation_with_original_core_completion_clock(tmp_path,monkeypatch):
    import queue
    from futures_trend_quant.live import paper_data,trend_observation
    from futures_trend_quant.core.models import Bar
    state=tmp_path/'state';state.mkdir();ev=tmp_path/'evidence';ev.mkdir()
    c=dict(root=str(tmp_path),state_dir=str(state),evidence_dir=str(ev),warmup_m15=1,base_config=SimpleNamespace(raw=dict(rules={})))
    worker=paper_data.SignalWorker(c,child=True)
    minutes=[Bar('DCE.m2701','60s',T-timedelta(minutes=15-i),T-timedelta(minutes=14-i),100,101,99,100) for i in range(15)]
    monkeypatch.setattr(paper_data,'CORE_MIN_M15',1)
    monkeypatch.setattr(paper_data,'run_core_step',lambda *a:SimpleNamespace(dummy=True))
    captured=[]
    def observe(result):
        assert result.dummy;return dict(algorithm_version='S0_TREND_OBSERVATION_V1',data_quality='OBSERVATION_ERROR')
    monkeypatch.setattr(trend_observation,'safe_from_result',observe)
    class Sink:
        def put(self,event):
            captured.append(event)
            if event[0]=='CORE_READY':worker.stop=True
    worker.outbox=Sink();worker.inbox.put(('NEW_BARS',minutes));worker._run()
    events=[e for e in captured if e[0]=='CORE_RESULT'];assert len(events)==1 and len(events[0])==5
    assert events[0][4]['data_quality']=='OBSERVATION_ERROR'
    assert datetime.fromisoformat(events[0][3]).tzinfo is not None
    assert any(e[0]=='CORE_READY' for e in captured)
