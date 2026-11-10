from pathlib import Path
from datetime import datetime
import hashlib,json
import pytest
from tools.paper_trend_notify import rows,POOL,deliver,priority_changes,trend_risk

NOW=datetime.fromisoformat('2026-11-05T21:15:20+08:00')


def test_authorized_trend_does_not_hide_existing_divergence_or_h1_veto():
    assert '顶背驰' in trend_risk(dict(direction='bull',evidence=dict(top_div=True)))
    assert '底背驰' in trend_risk(dict(direction='bear',evidence=dict(bottom_div=True)))
    assert 'H1强势否决' in trend_risk(dict(direction='bull',evidence=dict(top_div=True,trend_decision=dict(proposed_action='veto'))))
    assert trend_risk(dict(direction='bull',evidence={}))==''


def put(p,obj):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj),encoding='utf8')


def fixture(tmp_path):
    r=tmp_path/'_strategy_runtime';products={};contracts={}
    for cs,name in POOL:
        products[cs]=dict(symbol=cs+'ACTUAL',core=dict(last_finalized_h1='2026-11-05T15:00:00+08:00'))
        contracts[cs]=dict(csymbol=cs,trade_windows=[] if cs in ['DCE.JD','CZCE.CJ'] else [(NOW.timestamp()-3600,NOW.timestamp()+3600)])
        put(r/('state/paper_observation_five/products/'+cs.replace('.','_')+'/core_checkpoint.json'),dict(last_eob='2026-11-05T21:15:00+08:00',runtime=dict(trends={cs:dict(trend_state='bull',last_closed_bar_id='actual|900s|2026-11-05T21:15:00+08:00',leave_votes=0)})))
    put(r/'evidence/paper_observation_five/status.json',dict(at=NOW.isoformat(),products=products))
    put(r/'state/paper_observation_five/market_registry.local.json',dict(contracts=contracts))
    put(r/'config/paper_feishu.local.json',dict(enabled=False))
    return r


def test_no_signal_log_does_not_invent_no_path_and_resting_products_are_labelled(tmp_path):
    r=fixture(tmp_path);s,result=rows(r,NOW)
    assert len(result)==5 and all(x['label']=='偏多' for x in result)
    assert '尚未生成' in result[0]['reason']
    assert all('休市' in x['reason'] for x in result if x['csymbol'] in ['DCE.JD','CZCE.CJ'])


def test_only_matching_closed_bar_entry_and_facts_are_used(tmp_path):
    r=fixture(tmp_path);p=r/'evidence/paper_observation_five/signals.jsonl'
    p.write_text(json.dumps(dict(csymbol='DCE.M',bar_eob='2026-11-05T21:15:00+08:00',entry=dict(accepted=False,reason='NO_PATH'),facts=dict(ema34=100)))+'\n',encoding='utf8')
    result=rows(r,NOW)[1][0];assert result['reason']=='入场路径条件未到齐' and result['evidence']['ema34']==100
    p.write_text(json.dumps(dict(csymbol='DCE.M',bar_eob='2026-11-05T21:30:00+08:00',entry=dict(accepted=True),facts=dict(ema34=999)))+'\n',encoding='utf8')
    result=rows(r,NOW)[1][0];assert not result['evidence'] and '尚未生成' in result['reason']


def test_missing_and_future_checkpoints_never_default_to_a_direction(tmp_path):
    r=fixture(tmp_path);p=r/'state/paper_observation_five/products/DCE_M/core_checkpoint.json';p.unlink()
    assert rows(r,NOW)[1][0]['direction']=='UNKNOWN'
    put(p,dict(last_eob='2026-11-05T21:30:00+08:00',runtime=dict(trends={'DCE.M':dict(trend_state='bear',last_closed_bar_id='x|2026-11-05T21:30:00+08:00')})))
    assert rows(r,NOW)[1][0]['direction']=='UNKNOWN'


def test_stale_status_is_explicit_history_not_current_direction(tmp_path):
    r=fixture(tmp_path);p=r/'evidence/paper_observation_five/status.json';s=json.loads(p.read_text());s['at']='2026-11-05T20:00:00+08:00';put(p,s)
    assert all(not x['state_fresh'] and '陈旧' in x['reason'] for x in rows(r,NOW)[1])


def test_fresh_host_with_lagging_core_does_not_claim_current_bar(tmp_path):
    r=fixture(tmp_path);p=r/'state/paper_observation_five/products/DCE_M/core_checkpoint.json';s=json.loads(p.read_text());s['last_eob']='2026-11-05T21:00:00+08:00';s['runtime']['trends']['DCE.M']['last_closed_bar_id']='x|'+s['last_eob'];put(p,s)
    result=rows(r,NOW)[1][0]
    assert not result['state_fresh'] and '本轮M15尚未更新' in result['reason']


def test_cards_deduplicate_and_do_not_mutate_strategy_checkpoints(tmp_path):
    r=fixture(tmp_path);before={p:p.read_bytes() for p in (r/'state').rglob('*.json')}
    k=deliver(tmp_path,'FIXED_15M_SLOT','趋势判断',NOW);assert deliver(tmp_path,'FIXED_15M_SLOT','趋势判断',NOW)==k
    cards=list((r/'evidence/paper_observation_five/notifications').glob('*.json'));assert len(cards)==1
    assert json.loads(cards[0].read_text(encoding='utf8'))['status']=='LOCAL_ONLY_NOT_CONFIGURED'
    assert all(p.read_bytes()==blob for p,blob in before.items())


def test_summary_has_one_title_clock_and_colored_directions(tmp_path):
    r=fixture(tmp_path);key=deliver(tmp_path,'CLOCK','15分钟·五品种趋势判断',NOW)
    card=json.loads((r/'evidence/paper_observation_five/notifications'/f'{key}.json').read_text(encoding='utf8'))['text']['card']
    assert card['header']['title']['content'].endswith('截至 2026-11-05  21:15:00')
    body=json.dumps(card['elements'],ensure_ascii=False)
    assert '截至' not in body and 'T21:' not in body and '🟥 偏多' in body and '休市' in body


def source_change(r,old,new,*,eob='2026-11-05T21:15:00+08:00',key=None):
    from futures_trend_quant.live.paper_notify import message
    key=key or hashlib.sha256(('trend:DCE.M:'+eob).encode()).hexdigest()
    obj=dict(id=key,created_at=NOW.isoformat(),status='SENT',text=message('趋势方向变更',[],fields=[('标的','豆粕 DCE.MACTUAL'),('方向变化',old+' → '+new),('M15收盘时间',eob)],reasons=['已闭合H1 bull_strong，闭合时点 2026-11-05T15:00:00+08:00']))
    put(r/'evidence/paper_observation_five/notifications'/f'{key}.json',obj)
    return key


@pytest.mark.parametrize('old,new,color,word',[('bull','bear','red','趋势反转'),('bear','bull','red','趋势反转'),('bull','none','orange','趋势结束'),('none','bear','blue','新趋势确认')])
def test_confirmed_changes_get_distinct_priority_cards_and_deduplicate(tmp_path,old,new,color,word):
    r=fixture(tmp_path);assert priority_changes(tmp_path,NOW)==[]
    if old=='none':put(r/'evidence/paper_observation_five/trend_priority_cursor.local.json',dict(cutoff=NOW.isoformat(),processed=[],last_direction={}))
    source_change(r,old,new);result=priority_changes(tmp_path,NOW);assert len(result)==1
    card=json.loads((r/'evidence/paper_observation_five/notifications'/f'{result[0]}.json').read_text(encoding='utf8'))['text']['card']
    assert card['header']['template']==color and word in card['header']['title']['content']
    assert card['header']['title']['content'].endswith('截至 2026-11-05  21:15:00')
    assert 'UNKNOWN，持仓尚未完成核对' in json.dumps(card,ensure_ascii=False)
    assert 'T15:' not in json.dumps(card,ensure_ascii=False)
    assert priority_changes(tmp_path,NOW)==[]


def test_opposite_direction_after_neutral_is_establishment_not_direct_reversal(tmp_path):
    r=fixture(tmp_path);priority_changes(tmp_path,NOW);source_change(r,'bull','none');priority_changes(tmp_path,NOW)
    source_change(r,'none','bear',eob='2026-11-05T21:30:00+08:00')
    result=priority_changes(tmp_path,datetime.fromisoformat('2026-11-05T21:30:20+08:00'))
    assert len(result)==1
    card=json.loads((r/'evidence/paper_observation_five/notifications'/f'{result[0]}.json').read_text(encoding='utf8'))['text']['card']
    assert card['header']['template']=='blue' and '新趋势确认' in card['header']['title']['content']
    assert '趋势反转' not in json.dumps(card,ensure_ascii=False)


def test_priority_does_not_replay_startup_history_or_unmatched_or_future_events(tmp_path):
    r=fixture(tmp_path);source_change(r,'bull','bear')
    later=datetime.fromisoformat('2026-11-05T21:16:00+08:00')
    assert priority_changes(tmp_path,later)==[] and priority_changes(tmp_path,later)==[]
    state=r/'evidence/paper_observation_five/trend_priority_cursor.local.json';put(state,dict(cutoff=NOW.isoformat(),processed=[]))
    (r/'evidence/paper_observation_five/notifications').rename(r/'evidence/paper_observation_five/old_notifications')
    source_change(r,'bull','bear',key='f'*64)
    source_change(r,'bull','bear',eob='2026-11-05T21:30:00+08:00')
    assert priority_changes(tmp_path,NOW)==[]
