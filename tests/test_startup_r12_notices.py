"""Historical ledger length cannot multiply live proof reads before filtering."""
from datetime import timedelta
import json
import pytest
from test_paper_notice_realtime import fixture,put,NOW
from futures_trend_quant.live import paper_symbol_health as sh
from tools.paper_trend_notify import priority_divergences
from futures_trend_quant.live.trend_observation import event_id


@pytest.mark.parametrize('case',['no_event','old','processed','unconfirmed','market_bad','current'])
def test_real_notice_consumer_prefilters_history_before_any_market_io(tmp_path,monkeypatch,case):
    root,row=fixture(tmp_path);ev=root/'evidence/paper_observation_five';confirmed=row['bar_eob']
    event=dict(kind='top',mode='B_hist',previous_bar_id='DCE.m2701|900s|PREV',
               current_bar_id='DCE.m2701|900s|CURRENT',defined_at_bar_id='DCE.m2701|900s|'+confirmed,extreme_price=101)
    history=[]
    for i in range(1000):
        r=dict(row,facts=dict(row['facts']))
        # No current confirmed event: past history still needed for diagnosis.
        r['facts'].pop('top_divergence_event',None);history.append(r)
    r=dict(row,facts=dict(row['facts']))
    if case!='no_event':r['facts']['top_divergence_event']=event
    if case=='old':r['bar_eob']=(NOW-timedelta(minutes=30)).isoformat();event['defined_at_bar_id']='DCE.m2701|900s|'+r['bar_eob']
    if case=='unconfirmed':event['defined_at_bar_id']='DCE.m2701|900s|'+(NOW+timedelta(minutes=15)).isoformat()
    history.append(r);(ev/'signals.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in history),encoding='utf8')
    key='m15-divergence:DCE.M|'+event_id(event)
    put(ev/'m15_divergence_notice_cursor.local.json',dict(cutoff=(NOW-timedelta(minutes=5)).isoformat(),processed=[key] if case=='processed' else []))
    calls=[]
    def market(*a,**k):calls.append(a[1]);return dict(ready=case!='market_bad')
    monkeypatch.setattr(sh,'display_status',market)
    result=priority_divergences(tmp_path,NOW)
    assert len(calls)==int(case in ('market_bad','current'))
    assert len(result)==int(case=='current')
    if case=='current':assert priority_divergences(tmp_path,NOW)==[] and len(calls)==1
