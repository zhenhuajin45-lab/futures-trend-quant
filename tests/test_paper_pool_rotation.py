"""Pool authority reaches native submit and notification; state migration stays lossless."""
from copy import deepcopy
from pathlib import Path
import json
from types import SimpleNamespace
import pytest
from futures_trend_quant.live.paper_products import FIVE_POOL,OILS_POOL,OILS_POOL_VERSION,registered_pool,product_of
from tools import paper_pool_rotation as rotation


@pytest.mark.parametrize('symbol,cs',[('CZCE.OI701','CZCE.OI'),('DCE.y2701','DCE.Y'),('DCE.p2701','DCE.P')])
def test_new_actual_identity(symbol,cs):assert product_of(symbol)==cs


@pytest.mark.parametrize('symbol',['CZCE.oi701','CZCE.OI2701','DCE.Y2701','DCE.P','CZCE.OI','DCE.p2701x'])
def test_oils_reject_continuous_wrong_case_or_malformed(symbol):
    with pytest.raises(ValueError):product_of(symbol)


@pytest.mark.parametrize('universe,version',[(list(OILS_POOL),None),(list(OILS_POOL),'UNAPPROVED'),(list(FIVE_POOL)+['DCE.Y'],None),(['DCE.M','SHFE.AU','DCE.EG','DCE.JD','DCE.P'],OILS_POOL_VERSION),(list(reversed(OILS_POOL)),OILS_POOL_VERSION)])
def test_arbitrary_pool_cannot_bypass_authority(universe,version):
    with pytest.raises(ValueError):registered_pool(universe,version)


def test_original_and_new_pool_exact_authority():
    assert registered_pool(list(FIVE_POOL))==FIVE_POOL
    assert registered_pool(list(OILS_POOL),OILS_POOL_VERSION)==OILS_POOL
    assert registered_pool(['DCE.M'],allow_single=True)==('DCE.M',)


@pytest.mark.parametrize('symbol,cs',[('CZCE.OI701','CZCE.OI'),('DCE.y2701','DCE.Y'),('DCE.p2701','DCE.P')])
def test_real_bridge_new_pool_reaches_explicit_one_lot_native_submit(tmp_path,monkeypatch,symbol,cs):
    from test_paper_bridge import invoke,req,args,record,multi_profile
    a=args();a.update(symbol=symbol,anchor_bar_id=a['anchor_bar_id'].replace('DCE.m2701',symbol))
    profile=dict(multi_profile(),universe=list(OILS_POOL),pool_policy_version=OILS_POOL_VERSION)
    result,calls=invoke(tmp_path,monkeypatch,[req('submit',arguments=a)],profile_extra=profile,contracts={symbol:record(cs)})
    assert result[0]['ok']
    submits=[v for k,v in calls if k=='submit'];assert len(submits)==1 and submits[0]['symbol']==symbol and submits[0]['volume']==1


def test_removed_product_rejected_at_native_exit_even_if_metadata_present(tmp_path,monkeypatch):
    from test_paper_bridge import invoke,req,args,record,multi_profile
    a=args();a.update(symbol='DCE.eg2701',anchor_bar_id=a['anchor_bar_id'].replace('DCE.m2701','DCE.eg2701'))
    profile=dict(multi_profile(),universe=list(OILS_POOL),pool_policy_version=OILS_POOL_VERSION)
    result,calls=invoke(tmp_path,monkeypatch,[req('submit',arguments=a)],profile_extra=profile,contracts={'DCE.eg2701':record('DCE.EG')})
    assert not result[0]['ok'] and not any(k=='submit' for k,v in calls)


def test_trend_rows_and_cards_use_new_pool_not_legacy_constant(tmp_path):
    from test_paper_trend_notify import fixture,put,NOW
    from tools.paper_trend_notify import rows,deliver
    r=fixture(tmp_path);status=json.loads((r/'evidence/paper_observation_five/status.json').read_text())
    status.update(universe=list(OILS_POOL),pool_policy_version=OILS_POOL_VERSION)
    for cs in rotation.REMOVED:status['products'].pop(cs)
    for cs in rotation.ADDED:status['products'][cs]=dict(symbol={'CZCE.OI':'CZCE.OI701','DCE.Y':'DCE.y2701','DCE.P':'DCE.p2701'}[cs],core={})
    put(r/'evidence/paper_observation_five/status.json',status)
    result=rows(r,NOW)[1];assert [x['csymbol'] for x in result]==list(OILS_POOL)
    assert all(x['direction']=='UNKNOWN' for x in result if x['csymbol'] in rotation.ADDED)
    key=deliver(tmp_path,'NEW_POOL','五品种趋势方向总览',NOW)
    card=json.loads((r/'evidence/paper_observation_five/notifications'/f'{key}.json').read_text(encoding='utf8'))
    text=json.dumps(card,ensure_ascii=False);assert all(x in text for x in ['菜油','豆油','棕榈油'])
    assert all(x not in text for x in ['鸡蛋','红枣','乙二醇'])


def install_fixture(tmp_path,monkeypatch):
    client=tmp_path/'client';rt=client/'_strategy_runtime';st=rt/'state/paper_observation_five';ev=rt/'evidence/paper_observation_five';bundle=tmp_path/'batch';stage=bundle/'client'
    def put(p,d):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(d),encoding='utf8')
    put(client/'main.py',{'OLD':1});put(stage/'main.py',{'NEW':1})
    profile=Path('config/paper_observation_five.local.json');old=dict(universe=list(FIVE_POOL),risk_pct=.0025)
    put(rt/profile,old);put(stage/'_strategy_runtime'/profile,dict(old,universe=list(OILS_POOL),pool_policy_version=OILS_POOL_VERSION))
    state=dict(account_id='SYNTHETIC_ONLY',config_sha256='OLD',pause_new=False,halt_reason='',day_nav=101,peak_nav=105,used_signals=['DONE'],intents={},exec_ids=['EXEC'],loss_cooldown={'until':'PRESERVE'},connectivity_passed=True)
    put(st/'execution.json',state);cps={}
    for cs in FIVE_POOL:
        p=st/'products'/cs.replace('.','_');put(p/'core_checkpoint.json',dict(runtime={'event':'KEEP'},last_eob='FROZEN'))
        put(p/'contract_pin.json',dict(symbol='SYNTHETIC',config_sha256='OLD'));cps[cs]=(p/'core_checkpoint.json').read_bytes()
    warm={}
    for cs in rotation.ADDED:
        base=rt/'data/cache/paper_pool_rotation/batch';source=base/'state/products'/cs.replace('.','_');evidence=base/'evidence/products'/cs.replace('.','_');seed=rt/'data/cache/paper_market'/cs.replace('.','_')/'seed'
        seed.parent.mkdir(parents=True);seed.write_bytes(b'FROZEN_SEED');digest=rotation.prior.sha(seed)
        symbol={'CZCE.OI':'CZCE.OI701','DCE.Y':'DCE.y2701','DCE.P':'DCE.p2701'}[cs]
        newsha=rotation.prior.sha(stage/'_strategy_runtime'/profile)
        put(source/'core_checkpoint.json',dict(last_eob='CLOSED',runtime={'core_contract':'CLOSED_FACTS_V2'},seed_cache={'sha256':digest}))
        put(source/'contract_pin.json',dict(symbol=symbol,config_sha256=newsha));put(evidence/'market_metadata.local.json',dict(symbol=symbol,csymbol=cs))
        warm[cs]=dict(symbol=symbol,core_ready=True,coverage={'qualified':True,'expected_m15':1200,'missing_m1':0},seed_path=str(seed),sha256=digest,state_dir=str(source),evidence_dir=str(evidence),checkpoint_sha256=rotation.prior.sha(source/'core_checkpoint.json'),core={'last_eob':'CLOSED'})
    plan=dict(active_client=str(client),old_main_sha256_lf=rotation.prior.lfsha(client/'main.py'),new_main_sha256_lf=rotation.prior.lfsha(stage/'main.py'),old_config_sha256=rotation.prior.sha(rt/profile),new_config_sha256=newsha,new_core_sha256_lf='CORE')
    put(bundle/'release_plan.local.json',plan);put(bundle/'prewarm.local.json',dict(qualified=True,config_sha256=newsha,products=warm))
    put(bundle/'validation_gate.local.json',dict(targeted_tests_passed=True,native_quote_chain_passed=True,private_ledger_reconciled=True,core_unchanged=True,new_main_sha256_lf=plan['new_main_sha256_lf'],new_config_sha256=newsha))
    monkeypatch.setattr(rotation.prior,'flat_stopped',lambda _: (rt,ev,deepcopy(state),dict(ready_products=5)))
    monkeypatch.setattr(rotation,'frozen_core_digest',lambda _:'CORE');monkeypatch.setattr(rotation.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0))
    return client,bundle,state,cps,put


def test_install_archives_removed_keeps_retained_and_preserves_all_risk_history(tmp_path,monkeypatch):
    client,bundle,state,cps,put=install_fixture(tmp_path,monkeypatch);rotation.install(client,bundle)
    st=client/'_strategy_runtime/state/paper_observation_five';after=rotation.prior.read(st/'execution.json')
    for k,v in state.items():
        if k not in ('config_sha256','pause_new'):assert after[k]==v
    assert after['pause_new'] is True
    assert {p.name for p in (st/'products').iterdir()}=={cs.replace('.','_') for cs in OILS_POOL}
    for cs in rotation.RETAINED:assert (st/'products'/cs.replace('.','_')/'core_checkpoint.json').read_bytes()==cps[cs]
    for cs in rotation.REMOVED:assert (st/'retired_products'/bundle.name/cs.replace('.','_')/'core_checkpoint.json').read_bytes()==cps[cs]
    assert (bundle/'rollback/state/execution.json').exists()


@pytest.mark.parametrize('change',['seed','checkpoint','missing','risk','gate'])
def test_changed_prewarm_or_scope_rejected_before_touching_active_entry(tmp_path,monkeypatch,change):
    client,bundle,state,cps,put=install_fixture(tmp_path,monkeypatch);before=(client/'main.py').read_bytes();warm=rotation.prior.read(bundle/'prewarm.local.json');p=warm['products'][rotation.ADDED[0]]
    if change=='seed':Path(p['seed_path']).write_bytes(b'REVISED')
    if change=='checkpoint':Path(p['state_dir'],'core_checkpoint.json').write_bytes(b'REVISED')
    if change=='missing':p['coverage']['missing_m1']=1;put(bundle/'prewarm.local.json',warm)
    if change=='risk':
        path=bundle/'client/_strategy_runtime/config/paper_observation_five.local.json';d=rotation.prior.read(path);d['risk_pct']=.01;put(path,d)
    if change=='gate':put(bundle/'validation_gate.local.json',{})
    with pytest.raises(ValueError):rotation.install(client,bundle)
    assert (client/'main.py').read_bytes()==before and not (bundle/'rollback').exists()


def cycle_fixture(tmp_path):
    import queue,time
    from unittest.mock import Mock
    from futures_trend_quant.live.paper_five import FiveRunner
    from datetime import datetime,timezone
    r=FiveRunner.__new__(FiveRunner);r.max_gap=0.;r.last_loop=r.last_report=time.monotonic();r.last_poll=0
    r.c={'state_dir':str(tmp_path)};r.readonly=False;r.verified=False;r.fill_notified=set();r.last_problem=None
    alive=SimpleNamespace(is_alive=lambda:True)
    r.products={cs:dict(market=SimpleNamespace(thread=alive),core=SimpleNamespace(thread=alive,inbox=queue.Queue()),metadata=None,last_positions=None,fault=None,frozen=False,ready=True) for cs in OILS_POOL}
    r.quote_worker=alive;r.transport=SimpleNamespace(drain=lambda:[],hung=lambda:False,busy=lambda:True)
    r.calendar=SimpleNamespace(contracts={});r.event=Mock();r.notice=Mock();r.market_events=r.core_events=r.pump_notifications=lambda *a:None
    opening=dict(id='CJ_OPEN',effect=1,filled=1,tag='STRATEGY',csymbol='CZCE.CJ',symbol='CZCE.CJ701',direction='bull')
    closing=dict(opening,id='CJ_CLOSE',effect=2)
    state=dict(intents={'CJ_OPEN':opening,'CJ_CLOSE':closing},halt_reason='',pause_new=True)
    r.engine=SimpleNamespace(s=state,positions=lambda:[],active=lambda *a:[],save=Mock(),tick=lambda *a:None,drain_candidates=lambda *a:None,summary=lambda:{'new_entry_cooldown_reason':None},last_snapshot=datetime.now(timezone.utc).isoformat(),ready=True)
    return r


def test_actual_coordinator_cycle_preserves_closed_retired_fill_without_wrong_core_delivery(tmp_path):
    r=cycle_fixture(tmp_path);before=deepcopy(r.engine.s['intents'])
    assert r.cycle() is False
    assert r.engine.s['intents']==before and 'CJ_OPEN' in r.fill_notified
    assert all(all(job[0]!='FILL' for job in list(p['core'].inbox.queue)) for p in r.products.values())
    assert r.event.call_args[0][0]=='RETIRED_CLOSED_FILL_RETAINED'
    r.event.reset_mock();r.cycle();assert r.event.call_count==0


@pytest.mark.parametrize('case',['cumulative','position','active'])
def test_retired_exposure_never_silently_skipped(case,tmp_path):
    r=cycle_fixture(tmp_path)
    if case=='cumulative':r.engine.s['intents'].pop('CJ_CLOSE')
    if case=='position':r.engine.positions=lambda:[dict(symbol='CZCE.CJ701',volume=1)]
    if case=='active':r.engine.active=lambda *a:[dict(symbol='CZCE.CJ701')]
    with pytest.raises(ValueError,match='retired product'):r.restore_strategy_fills()
    assert r.engine.s['pause_new'] and 'CJ_OPEN' not in r.fill_notified


def test_active_pool_fill_still_reaches_own_core_once(tmp_path):
    r=cycle_fixture(tmp_path);r.engine.s['intents']={'Y':dict(id='Y',effect=1,filled=1,tag='STRATEGY',csymbol='DCE.Y',symbol='DCE.y2701',direction='bull')}
    r.restore_strategy_fills();r.restore_strategy_fills()
    assert r.products['DCE.Y']['core'].inbox.qsize()==1
    assert r.products['DCE.Y']['core'].inbox.get()[0]=='FILL'
