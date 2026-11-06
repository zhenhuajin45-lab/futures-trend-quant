"""Local-only fixed SEEN-input semantic review. No SDK/network/order imports.

prepare freezes copies before replay; replay reads only the approved frozen copies.
Raw M1, checkpoints and full traces stay in ignored evidence/, never in reports/.
"""
from pathlib import Path
from datetime import datetime,timedelta,timezone
from dataclasses import asdict
from collections import Counter
import argparse,csv,hashlib,html,json,shutil,time
import pandas as pd
from futures_trend_quant.core.models import Bar
from futures_trend_quant.core.pipeline import CoreStepInput,StrategyRuntimeBook,run_core_step
from futures_trend_quant.live.trend_observation import from_result,build,event_id
from futures_trend_quant.config import load_config
from backtest.aggregation import combine_bars

ROOT=Path(__file__).resolve().parents[1]
TZ=timezone(timedelta(hours=8));START=datetime(2026,11,5,21,tzinfo=TZ);END=datetime(2026,11,6,15,tzinfo=TZ)
POOL=['DCE.M','SHFE.AU','DCE.EG','DCE.JD','CZCE.CJ']


def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def lfsha(p):return hashlib.sha256(Path(p).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
def write(p,value):
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf8')


def core_hash(root):
    h=hashlib.sha256()
    for p in sorted((root/'futures_trend_quant/core').rglob('*.py'))+[root/'tools/short_swing_v9/policy.py']:
        h.update(str(p.relative_to(root)).replace('\\','/').encode()+b'\0');h.update(p.read_bytes().replace(b'\r\n',b'\n')+b'\0')
    return h.hexdigest()


def prepare(client,out):
    rt=client/'_strategy_runtime'
    if out.exists():raise ValueError('use a new immutable review batch')
    out.mkdir(parents=True);s=read(rt/'evidence/paper_observation_five/status.json')
    cp=rt/'config/paper_observation_five.local.json';config=read(cp)
    # Only hashes and approved risk fields, never account/token/webhook content.
    baseline=dict(at=datetime.now(TZ).isoformat(),entry_sha256_lf=lfsha(client/'main.py'),config_sha256=sha(cp),
        repository_core_sha256_lf=core_hash(ROOT),runtime_core_sha256_lf=core_hash(rt),
        actual_host=read(rt/'evidence/paper_observation_five/gm_platform_host.local.json'),
        policies={k:config.get(k) for k in ('strategy_name','permission_policy','notification_policy_version','execution_policy_version')},
        risk={k:config[k] for k in ('risk_pct','daily_loss_pct','observation_drawdown_pct','margin_usage_pct','max_lots','max_positions')},
        status=dict(at=s['at'],phase=s['phase'],ready=s['ready_products'],broker=s['broker']['status'],
                    active=s['broker']['active_intents'],positive_positions=sum(int(p['volume'])>0 for p in s['broker']['positions']),
                    pause_new=s['broker']['pause_new'],halt_reason=s['broker']['halt_reason']),
        review='SINGLE_AGENT_SELF_CHECK',permission_changed=False,independent_sample_consumed=False)
    write(out/'baseline_lock.json',baseline)
    copies={'strategy':rt/'config/strategy.json','registry':rt/'state/paper_observation_five/market_registry.local.json',
            'signals':rt/'evidence/paper_observation_five/signals.jsonl'}
    items=[]
    for cs in POOL:
        folder=cs.replace('.','_');p=rt/('state/paper_observation_five/products/'+folder+'/core_checkpoint.json');book=read(p)
        seed=Path(book['seed_cache']['path']).resolve()
        if not seed.is_relative_to(rt/'data/cache/paper_market') or sha(seed)!=book['seed_cache']['sha256']:raise ValueError('seed identity changed')
        copies[folder+'_checkpoint']=p;copies[folder+'_seed']=seed
        copies[folder+'_observed']=rt/('evidence/paper_observation_five/products/'+folder+'/m1_observed.jsonl')
    for name,p in copies.items():
        suffix='.parquet' if name.endswith('_seed') else '.jsonl' if name.endswith('_observed') or name=='signals' else '.json'
        target=out/'inputs'/(name+suffix);target.parent.mkdir(exist_ok=True)
        # Hash the frozen bytes, not a file that can grow while the host runs.
        data=p.read_bytes();target.write_bytes(data)
        items.append(dict(logical_name=name,path=str(target.relative_to(out)),bytes=len(data),sha256=sha(target),
                          original_path=str(p),purpose='SEEN_ENGINEERING_CASE;WARMUP_ALREADY_OBSERVED',arrival='DIRECT_RECORD_ONLY_ELSE_UNKNOWN'))
    # Freeze the exact prior fixed gold incident prefix independently of today's seed.
    prior=Path(r'E:/掘金/期货-核心修复/evidence/core_v2_final_replay')
    for name in ('gold_replay.local.json','chart_sources.local.json','runtime_final.local.json'):
        target=out/'inputs'/name;shutil.copyfile(prior/name,target)
        items.append(dict(logical_name=name,path=str(target.relative_to(out)),bytes=target.stat().st_size,sha256=sha(target),purpose='PRIOR_FIXED_V2_INCIDENT_REPLAY'))
    source=read(out/'inputs/gold_replay.local.json')['source'];raw=(rt/'evidence/paper_observation_five/products/SHFE_AU/m1_observed.jsonl').read_bytes()[:source['observed_prefix_bytes']]
    if hashlib.sha256(raw).hexdigest()!=source['observed_prefix_sha256']:raise ValueError('prior original gold prefix changed')
    write(out/'input_manifest.json',dict(scope_start=START,scope_end=END,protected_samples_read=False,
        allowed_inputs=items,prior_gold_original_prefix_verified=True,response_protocol_sha256=sha(ROOT/'research/futures_trend_observer/response_protocol.json')))
    print(json.dumps(dict(prepared=True,inputs=len(items),core_equal=baseline['repository_core_sha256_lf']==baseline['runtime_core_sha256_lf'])),flush=True)


def complete_bars(minutes,period):
    groups={};width=period*60
    for b in minutes:
        t=int(b.eob.timestamp());end=(t+width-1)//width*width;groups.setdefault(end,[]).append(b)
    result=[]
    for end,items in sorted(groups.items()):
        items.sort(key=lambda x:x.eob)
        if len(items)==period and all(int(b.eob.timestamp())==end-(period-1-j)*60 for j,b in enumerate(items)):
            result.append(combine_bars(items,str(width)+'s'))
    return result


def load_minutes(out,cs,volume_revisions=None):
    name=cs.replace('.','_');seed=pd.read_parquet(out/'inputs'/f'{name}_seed.parquet').to_dict('records')
    observed=[json.loads(l) for l in (out/'inputs'/f'{name}_observed.jsonl').read_text(encoding='utf8').splitlines()]
    minutes={};received={};duplicates=0
    annex=out/'input_annex.json';extra=[]
    if annex.exists():
        for item in read(annex)['files']:
            if item['csymbol']!=cs:continue
            if sha(out/item['path'])!=item['sha256']:raise ValueError('input annex changed')
            extra += [r for r in pd.read_parquet(out/item['path']).to_dict('records') if START<pd.Timestamp(r['eob']).to_pydatetime()<=END]
    for row_number,row in enumerate(seed+observed+extra):
        eob=pd.Timestamp(row['eob']).to_pydatetime()
        if eob>END:continue
        b=Bar(row['symbol'],'60s',pd.Timestamp(row['bob']).to_pydatetime(),eob,*[float(row[k]) for k in ('open','high','low','close')],float(row.get('volume',0)))
        if b.eob in minutes:
            old=minutes[b.eob]
            if (old.symbol,old.open,old.high,old.low,old.close)!=(b.symbol,b.open,b.high,b.low,b.close):
                raise ValueError('closed original M1 conflict: '+str(b.eob))
            if old.volume!=b.volume:
                if row_number<len(seed)+len(observed):raise ValueError('original observed M1 volume conflict: '+str(b.eob))
                if volume_revisions is not None:
                    volume_revisions.append(dict(csymbol=cs,eob=b.eob.isoformat(),original_volume=old.volume,later_cache_volume=b.volume,
                        policy='KEEP_EARLIER_FROZEN_OBSERVATION;OHLC_IDENTICAL;NO_VOLUME_SIGNAL_CONCLUSION'))
            duplicates+=1
        else:minutes[b.eob]=b
        if row.get('local_observed_at'):
            received[b.eob]=min(received.get(b.eob,row['local_observed_at']),row['local_observed_at'])
    return sorted(minutes.values(),key=lambda x:x.eob),received,duplicates


def write_csv(path,rows):
    if not rows:return
    path.parent.mkdir(parents=True,exist_ok=True)
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader()
        for row in rows:w.writerow({k:json.dumps(v,ensure_ascii=False,sort_keys=True,default=str) if isinstance(v,(dict,list)) else v for k,v in row.items()})


def actual_clock(contract):
    return [datetime.fromtimestamp(t,TZ) for a,b in contract['trade_windows'] for t in range(int(a)+60,int(b)+1,60)]


def response(minutes,legal,eob,direction,reference,n):
    future=[t for t in legal if t>eob][:n]
    if direction not in ('bull','bear'):return dict(status='UNKNOWN_NO_DIRECTION')
    if len(future)!=n or any(t not in minutes for t in future):return dict(status='UNKNOWN_MISSING_OR_UNFINISHED')
    bs=[minutes[t] for t in future];sign=1 if direction=='bull' else -1;entry=bs[0].open
    return dict(status='COMPLETE_DESCRIPTIVE_ONLY',signal_to_next_open=sign*(entry-reference),
        signal_displacement=sign*(bs[-1].close-reference),after_next_open_displacement=sign*(bs[-1].close-entry),
        mfe=max(sign*((b.high if sign>0 else b.low)-entry) for b in bs),
        mae=min(sign*((b.low if sign>0 else b.high)-entry) for b in bs),entry_proxy=entry,entry_proxy_eob=future[0].isoformat())


def replay(out,products=None):
    manifest=read(out/'input_manifest.json')
    for item in manifest['allowed_inputs']:
        if sha(out/item['path'])!=item['sha256']:raise ValueError('frozen review input changed')
    rules=dict(load_config(out/'inputs/strategy.json').raw['rules'],paper_permission_v1=True)
    registry=read(out/'inputs/registry.json')['contracts'];all_rows=[];audit=[];coverage=[];summaries=[];history_rows=[];started=time.monotonic()
    actual=[json.loads(l) for l in (out/'inputs/signals.jsonl').read_text(encoding='utf8').splitlines()]
    expected_actual={}
    for r in actual:
        if (r.get('facts') or {}).get('trade_permission'):expected_actual[(r['csymbol'],r['bar_eob'])]=r
    for cs in products or POOL:
        bars,received,dups=load_minutes(out,cs);m15=complete_bars(bars,15);m5=complete_bars(bars,5);book=StrategyRuntimeBook();product_rows=[]
        contract=registry[bars[-1].symbol];legal=actual_clock(contract);by_minute={b.eob:b for b in bars}
        episode_history=[]
        for i,bar in enumerate(m15):
            if i<250:continue
            result=run_core_step(CoreStepInput(cs,bar.symbol,tuple(m15[max(0,i+1-1200):i+1]),
                       tuple(b for b in m5 if b.eob<=bar.eob)[-1200:],tuple(b for b in bars if b.eob<=bar.eob)[-1200:],rules),book)
            episode_history.append((bar.eob.isoformat(),result.trend.state.since_bar_id))
            if START<bar.eob<=END:
                o=from_result(result);o['review_role']='OFFLINE_SEEN_DIAGNOSTIC';o['first_received_at']=received.get(bar.eob)
                f=result.facts_row;p=f['trade_permission'];entry=asdict(result.entry) if result.entry else None
                source=expected_actual.get((cs,bar.eob.isoformat()));recorded_permission=(source or {}).get('facts',{}).get('trade_permission')
                same_permission=None if recorded_permission is None else recorded_permission==json.loads(json.dumps(p,default=str))
                e15=[t for t in legal if bar.bob<t<=bar.eob]
                o['is_complete']=len(e15)==15 and all(t in by_minute for t in e15)
                if not o['is_complete']:o['data_quality']='CALENDAR_UNQUALIFIED';o['phase']='UNKNOWN'
                o.update(csymbol=cs,close=bar.close,ema34=f['ema34'],atr14=f['atr14'],h1_source=f['h1_source'],core_facts=f,
                         production_permission=p,recorded_permission_equal=same_permission,
                         account_replayed=False,entry=entry,recorded_entry=(source or {}).get('entry'),trend_decision=f['trend_decision'])
                all_rows.append(o);product_rows.append(o)
                events=o['divergence_events'];both=bool(f['top_div'] and f['bottom_div'])
                for e in events:
                    episode=o['episode_id'];known=e['event_confirmed_at']
                    seen_episodes={eid for when,eid in episode_history if when>=known and eid}
                    # Prefix observed in this replay is an explicit lower bound, not invented local waves.
                    audit.append(dict(csymbol=cs,symbol=bar.symbol,eob=bar.eob.isoformat(),**e,episode_id=episode,
                        cross_episode_count_lower_bound=max(0,len(seen_episodes)-1),episode_scope='EXISTING_CONFIRMED_S0_TREND_ONLY;LOCAL_EPISODE_UNKNOWN',
                        both_counterfactual_directions_divergence_vetoable=both,
                        actual_background=result.trend.confirmed_state,actual_new_entry_allowed=p['new_entry_allowed'],
                        permission_reasons=p['reasons'],only_divergence_blocks=p['reasons']==['M15_OPPOSITE_DIVERGENCE_ACTIVE'],
                        candidate_path=None if not entry else entry['path'],entry_reason=None if not entry else entry['reason']))
                responses={str(n):response(by_minute,legal,bar.eob,result.trend.confirmed_state,bar.close,n) for n in (15,30,60,120)}
                coverage.append(dict(csymbol=cs,eob=bar.eob.isoformat(),phase=o['phase'],background=result.trend.confirmed_state,
                    recorded_entry=(source or {}).get('entry'),flat_replay_entry=entry,permissions_changed=False,released_by_observer=False,responses=responses))
            if i%150==0:
                write(out/'progress'/cs.replace('.','_')/(str(i)+'.local.json'),dict(index=i,eob=bar.eob,core_sha256_lf=core_hash(ROOT),runtime=book.to_dict()))
                print(json.dumps(dict(csymbol=cs,processed=i,total=len(m15),elapsed=round(time.monotonic()-started))),flush=True)
        for scope,state in book.facts.by_csymbol.items():
            for event in state.divergences:
                key='|'.join((event.kind,event.mode,event.previous_bar_id,event.current_bar_id))
                history_rows.append(dict(csymbol=cs,scope=scope,event_id=key,**asdict(event),
                    tombstone=state.invalidated.get(key),structural_valid=key not in state.invalidated,
                    warning_active=None,policy_selection='LATEST_OF_KIND_ONLY;DO_NOT_REACTIVATE_OLDER_EVENTS'))
        cp=read(out/'inputs'/(cs.replace('.','_')+'_checkpoint.json'))
        final_cp=cp['runtime']['trends'][cs];replay_trend=book.trends[cs].to_dict()
        expected_minutes=[t for t in legal if START<t<=END];missing=[t.isoformat() for t in expected_minutes if t not in by_minute]
        summaries.append(dict(csymbol=cs,symbol=bars[-1].symbol,m1=len(bars),m15=len(m15),case_rows=len(product_rows),duplicates_consistent=dups,
            expected_case_m1=len(expected_minutes),missing_case_m1=len(missing),missing_intervals=missing,
            same_last_eob=cp['last_eob']==m15[-1].eob.isoformat(),final_trend_equal=replay_trend==final_cp,
            recorded_permission_comparisons=sum(r['recorded_permission_equal'] is not None for r in product_rows),
            recorded_permission_mismatches=sum(r['recorded_permission_equal'] is False for r in product_rows),
            phases=dict(Counter(r['phase'] for r in product_rows)),local_direction_unknown=len(product_rows),
            warmup_contract='EXISTING_1200_COMPLETE_M15;CORE_MIN251_STATE_PREFIX',input_prefix_m15=len(m15)))
    target=out if products is None else out/'partials'/products[0].replace('.','_')
    # Save every expensive computed component before any presentation exporter.
    # A CSV/HTML formatting failure must not discard audit/lifecycle evidence.
    write(target/'computed_parts.local.json',dict(rows=all_rows,audit=audit,coverage=coverage,history=history_rows,summaries=summaries))
    write(target/'all_states.local.json',all_rows);write_csv(target/'all_states.csv',all_rows);write_csv(target/'divergence_veto_audit.csv',audit)
    write(target/'audit.local.json',audit);write(target/'history.local.json',history_rows)
    write_csv(target/'candidate_coverage.csv',coverage);write(target/'candidate_coverage.local.json',coverage);write(target/'data_quality.local.json',summaries)
    write_csv(target/'event_history.csv',history_rows)
    summary=dict(scope='SEEN_ENGINEERING_SEMANTICS_ONLY',core_sha256_lf=core_hash(ROOT),production_permission_changed=False,
        rows=len(all_rows),products=summaries,audit_rows=len(audit),warnings_policy='NOT_REGISTERED;UNKNOWN',
        actual_veto_rows=sum(r['entry_veto_active'] is True for r in all_rows),
        both_counterfactual_vetoable_rows=len({(r['csymbol'],r['eob']) for r in audit if r['both_counterfactual_directions_divergence_vetoable']}),
        only_divergence_blocks_rows=len({(r['csymbol'],r['eob']) for r in audit if r['only_divergence_blocks']}),
        release_count=0,independent_sample_consumed=False,orders_sent=0,review='SINGLE_AGENT_SELF_CHECK',
        elapsed_seconds=round(time.monotonic()-started,2))
    write(target/'review_summary.local.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k!='products'}),flush=True)
    return summary


def merge(out):
    all_rows=[];audit=[];coverage=[];history_rows=[];summaries=[]
    for cs in POOL:
        folder=out/'partials'/cs.replace('.','_');summary=read(folder/'review_summary.local.json')
        if summary['core_sha256_lf']!=core_hash(ROOT):raise ValueError('partial core version mismatch')
        all_rows+=read(folder/'all_states.local.json');audit+=read(folder/'audit.local.json')
        history_rows+=read(folder/'history.local.json');coverage+=read(folder/'candidate_coverage.local.json');summaries+=summary['products']
    all_rows.sort(key=lambda r:(r['bar_eob'],r['csymbol']));audit.sort(key=lambda r:(r['eob'],r['csymbol'],r['kind']))
    write(out/'all_states.local.json',all_rows);write_csv(out/'all_states.csv',all_rows);write_csv(out/'divergence_veto_audit.csv',audit)
    write_csv(out/'candidate_coverage.csv',coverage);write(out/'candidate_coverage.local.json',coverage);write(out/'data_quality.local.json',summaries)
    write_csv(out/'event_history.csv',history_rows)
    summary=dict(scope='SEEN_ENGINEERING_SEMANTICS_ONLY',core_sha256_lf=core_hash(ROOT),products=summaries,rows=len(all_rows),audit_rows=len(audit),
        actual_veto_rows=sum(r['entry_veto_active'] is True for r in all_rows),
        both_counterfactual_vetoable_rows=len({(r['csymbol'],r['eob']) for r in audit if r['both_counterfactual_directions_divergence_vetoable']}),
        only_divergence_blocks_rows=len({(r['csymbol'],r['eob']) for r in audit if r['only_divergence_blocks']}),
        release_count=0,production_permission_changed=False,independent_sample_consumed=False,orders_sent=0,review='SINGLE_AGENT_SELF_CHECK')
    write(out/'review_summary.local.json',summary);print(json.dumps({k:v for k,v in summary.items() if k!='products'}),flush=True)


def recover_exports(out):
    """Finish serialization from already computed immutable rows, never rerun/select results."""
    registry=read(out/'inputs/registry.json')['contracts']
    for cs in POOL:
        folder=out/'partials'/cs.replace('.','_');rows=read(folder/'all_states.local.json')
        bars,received,dups=load_minutes(out,cs);m15=complete_bars(bars,15);by_minute={b.eob:b for b in bars}
        legal=actual_clock(registry[bars[-1].symbol]);audit=[];coverage=[];history=[]
        for o in rows:
            p=o['production_permission'];events=o['divergence_events'];both={e['kind'] for e in events}=={'top','bottom'}
            for e in events:
                episodes={r['episode_id'] for r in rows if r['bar_eob']>=e['event_confirmed_at'] and r['bar_eob']<=o['bar_eob'] and r['episode_id']}
                audit.append(dict(csymbol=cs,symbol=o['actual_symbol'],eob=o['bar_eob'],**e,episode_id=o['episode_id'],
                    cross_episode_count_lower_bound=max(0,len(episodes)-1),episode_scope='RECORDED_CASE_WINDOW_LOWER_BOUND;EARLIER_WARMUP_TRANSITIONS_NOT_EXPORTED',
                    both_counterfactual_directions_divergence_vetoable=both,actual_background=o['production_m15_background'],
                    actual_new_entry_allowed=p['new_entry_allowed'],permission_reasons=p['reasons'],
                    only_divergence_blocks=p['reasons']==['M15_OPPOSITE_DIVERGENCE_ACTIVE'],
                    candidate_path=None if not o['entry'] else o['entry']['path'],entry_reason=None if not o['entry'] else o['entry']['reason']))
            coverage.append(dict(csymbol=cs,eob=o['bar_eob'],phase=o['phase'],background=o['production_m15_background'],
                recorded_entry=o['recorded_entry'],flat_replay_entry=o['entry'],permissions_changed=False,released_by_observer=False,
                responses={str(n):response(by_minute,legal,datetime.fromisoformat(o['bar_eob']),o['production_m15_background'],o['close'],n) for n in (15,30,60,120)}))
        cp=read(out/'inputs'/(cs.replace('.','_')+'_checkpoint.json'))
        for scope,state in cp['runtime']['facts']['by_csymbol'].items():
            for event in state['divergences']:
                key=event_id(event)
                history.append(dict(csymbol=cs,scope=scope,event_id=key,**event,tombstone=state['invalidated'].get(key),
                    structural_valid=key not in state['invalidated'],warning_active=None,source='FROZEN_ACTUAL_CORE_CHECKPOINT',
                    policy_selection='LATEST_OF_KIND_ONLY;DO_NOT_REACTIVATE_OLDER_EVENTS'))
        expected=[t for t in legal if START<t<=END];missing=[t.isoformat() for t in expected if t not in by_minute]
        summaries=[dict(csymbol=cs,symbol=bars[-1].symbol,m1=len(bars),m15=len(m15),case_rows=len(rows),duplicates_consistent=dups,
            expected_case_m1=len(expected),missing_case_m1=len(missing),missing_intervals=missing,
            same_last_eob=cp['last_eob']==m15[-1].eob.isoformat(),
            final_trend_equal=rows[-1]['trend_decision']['state']==cp['runtime']['trends'][cs],
            recorded_permission_comparisons=sum(r['recorded_permission_equal'] is not None for r in rows),
            recorded_permission_mismatches=sum(r['recorded_permission_equal'] is False for r in rows),
            phases=dict(Counter(r['phase'] for r in rows)),local_direction_unknown=len(rows),
            complete_m15_before_first_case=sum(b.eob<=datetime.fromisoformat(rows[0]['bar_eob']) for b in m15)-1,
            input_prefix_m15=len(m15),warmup_contract='EXISTING_1200_COMPLETE_M15;CORE_MIN251_STATE_PREFIX')]
        write_csv(folder/'all_states.csv',rows);write_csv(folder/'divergence_veto_audit.csv',audit);write(folder/'audit.local.json',audit)
        write(folder/'history.local.json',history);write_csv(folder/'event_history.csv',history)
        write_csv(folder/'candidate_coverage.csv',coverage);write(folder/'candidate_coverage.local.json',coverage)
        write(folder/'review_summary.local.json',dict(core_sha256_lf=core_hash(ROOT),products=summaries,
            export_recovered_from_saved_rows=True,observation_recomputed=False,orders_sent=0))
    merge(out)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','replay','merge','recover-exports']);p.add_argument('--client',type=Path);p.add_argument('--product',choices=POOL);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.mode=='prepare':prepare(a.client.resolve(),a.output.resolve())
    elif a.mode=='merge':merge(a.output.resolve())
    elif a.mode=='recover-exports':recover_exports(a.output.resolve())
    else:replay(a.output.resolve(),[a.product] if a.product else None)
