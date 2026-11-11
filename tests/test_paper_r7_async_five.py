"""Schema2/current five-pool, V5 B short -> next real cycle -> final fake native.

These are production-shaped synthetic fixtures, not a replay of historical fills.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
import queue,time
import pandas as pd
import pytest
from futures_trend_quant.live.paper_five import FiveRunner
from futures_trend_quant.live.paper_products import OILS_POOL,OILS_POOL_VERSION
from futures_trend_quant.live.paper_config import digest
from test_paper_service_health import runner
from test_paper_runtime_health_v6 import setup_health,write
from test_paper_entry_v5 import v5_signal
from test_paper_execution import NOW
from test_paper_bridge import invoke,req


def short_signal(symbol,cs):
    s=v5_signal(NOW)
    def convert(value):
        if isinstance(value,str):return value.replace('DCE.m2701',symbol).replace('|bull|','|bear|').replace('B|bull|','B|bear|')
        if isinstance(value,dict):return {k:convert(v) for k,v in value.items()}
        if isinstance(value,list):return [convert(v) for v in value]
        return value
    s=convert(s);s.update(csymbol=cs,direction='bear',permission_direction='bear',anchor=103,reference_opponent_price=100)
    s['entry_policy_proof'].update(close_side='below',slope='down',hist=1,h1_absolute='bear_strong')
    return s


@pytest.mark.parametrize('symbol,cs,tick,mult', [('DCE.p2701','DCE.P',2,10),('CZCE.OI701','CZCE.OI',1,10)])
@pytest.mark.parametrize('shadow_fault',[False,True])
def test_current_five_pool_strict_b_short_own_wal_not_self_blocked_at_final_native(tmp_path,monkeypatch,symbol,cs,tick,mult,shadow_fault):
    r,t=runner(tmp_path/'engine',at=NOW);r.c.update(schema_version=2,universe=list(OILS_POOL),pool_policy_version=OILS_POOL_VERSION,
        strategy_name='S0_SHORT_PAPER_CORE_V2',entry_policy_version='PAPER_ENTRY_V5',execution_policy_version='PAPER_EXEC_V5')
    s=short_signal(symbol,cs)
    # Original fixtures use a 1-tick book; actual contract tick is explicit.
    s.update(tick=tick,multiplier=mult)
    r.engine.quote(symbol,dict(created_at=str(NOW),last=100,bid=100,ask=100+tick,tick=tick,bid_size=5,ask_size=5),NOW)
    requests=[];created={}
    extra=dict(schema_version=2,universe=list(OILS_POOL),pool_policy_version=OILS_POOL_VERSION,market_registry='market.json',max_positions=2,
        strategy_name='S0_SHORT_PAPER_CORE_V2',entry_policy_version='PAPER_ENTRY_V5',execution_policy_version='PAPER_EXEC_V5',
        clock_policy_version='PAPER_CLOCK_HEALTH_V1_20261109',state_dir=r.c['state_dir'],evidence_dir=r.c['evidence_dir'])
    contracts={symbol:dict(csymbol=cs,tick=tick,multiplier=mult,margin_ratio=.07,
        trade_windows=[(NOW.timestamp()-3600,NOW.timestamp()+3600)],entry_windows=[(NOW.timestamp()-3600,NOW.timestamp()+3600)])}
    def before(root,profile):
        r.c['config_sha256']=digest(profile);r.engine.s['config_sha256']=digest(profile);r.engine.save()
        setup_health(r.c);r.run_id='CURRENT'
        assert r.engine.offer(s,NOW)[1]=='CANDIDATE_WAITING'
        r.engine.drain_candidates(NOW);created.update(t.jobs[-1]);job=t.jobs[-1]
        r.transport.writer=SimpleNamespace(health=lambda:dict(state='PENDING',op='submit',rpc_id=job['id']))
        write(r.evidence/('latest_quotes/'+symbol+'.json'),dict(symbol=symbol,config_sha256=digest(profile),received_at=str(NOW),
            quote=dict(r.engine.quotes[symbol],source='GM_HOST_TICK_CALLBACK')))
        r.c['quote_policy_version']='GM_HOST_TICK_PRIMARY_V1_20261109'
        # The native profile uses the same accepted feed policy.
        requests.append(req('submit',job['id'],arguments=job['args']))
    extra['quote_policy_version']='GM_HOST_TICK_PRIMARY_V1_20261109'
    def next_cycle():
        alive=SimpleNamespace(is_alive=lambda:True)
        for p in r.products.values():p.update(market=SimpleNamespace(thread=alive),core=SimpleNamespace(thread=alive,inbox=queue.Queue()),metadata=None,last_positions={})
        r.transport.drain=lambda:[];r.transport.busy=lambda:True;r.transport.hung=lambda:False
        r.apply_transport=lambda:FiveRunner.apply_transport(r)
        r.transport.reader_available=lambda:True;r.transport.reader.hung=lambda:False
        r.engine.transport=r.transport;r.transport.request=lambda *a,**k:None
        r.quote_worker=alive;r.last_loop=time.monotonic();r.max_gap=0;r.readonly=False
        r.market_events=Mock();r.core_events=Mock();r.restore_strategy_fills=Mock();r.pump_notifications=Mock();r.supervise_clock=Mock()
        r.last_problem=None;r.last_poll=time.monotonic();r.last_report=time.monotonic()
        r.price_action_observer=SimpleNamespace(offer=Mock(side_effect=queue.Full() if shadow_fault else None),dropped=0)
        FiveRunner.observe(r,'CANDIDATE',csymbol=cs,candidate=s)
        monkeypatch.setattr('futures_trend_quant.live.paper_five.pd',SimpleNamespace(Timestamp=SimpleNamespace(now=lambda **kw:NOW+pd.Timedelta(seconds=.2))))
        FiveRunner.cycle(r)
        assert r.service_health.state['pending_entry_request']['request_id']==created['id']
    class Clock:
        fromisoformat=staticmethod(datetime.fromisoformat)
        @staticmethod
        def now(tz):return (NOW+pd.Timedelta(seconds=.3)).to_pydatetime()
    replies,calls=invoke(tmp_path,monkeypatch,requests,profile_extra=extra,contracts=contracts,before_serve=before,cash_hook=next_cycle,clock=Clock)
    assert replies[0]['ok'] and replies[0]['native_submit_invoked'] is True,replies[0]
    submitted=[row for name,row in calls if name=='submit']
    assert len(submitted)==1 and submitted[0]['symbol']==symbol and submitted[0]['side']==2 and submitted[0]['volume']==1
    assert created['args']['expires_at']==s['expires_at']
    reply=replies[0];reply['data'][0].update(status=1,filled_volume=0,filled_vwap=0,side=2,position_effect=1,created_at=str(NOW),updated_at=str(NOW))
    r.engine.accept(created,reply,NOW+pd.Timedelta(seconds=.4))
    assert r.engine.s['intents'][created['id']]['filled']==0
    assert r.engine.open(s,NOW+pd.Timedelta(seconds=.5))[1]=='DUPLICATE_SIGNAL'
