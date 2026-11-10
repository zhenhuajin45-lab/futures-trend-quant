"""Audit frozen local originals only; no SDK, network or execution import."""
from pathlib import Path
from datetime import datetime
from collections import Counter
import argparse
import hashlib
import io
import json

from futures_trend_quant.core.facts_state import FactsState, divergence_event_id
from futures_trend_quant.core.models import TrendState
from futures_trend_quant.core.trend.state_machine import TrendFacts, step_trend
from futures_trend_quant.core.trend.divergence_shadow import advance
from futures_trend_quant.live.paper_calendar import PaperCalendar


def audit(root, output):
    import pandas as pd
    manifest=json.loads((root/'freeze_manifest.local.json').read_text(encoding='utf8'))
    index={x['file']:x for x in manifest['files']}
    prefix='_strategy_runtime/'
    def read(name):
        row=index[prefix+name]
        blob=(root/row['object'] if row.get('object') else root/'originals'/row['file']).read_bytes()
        if hashlib.sha256(blob).hexdigest()!=row['sha256']:raise ValueError('frozen prefix changed')
        return blob
    def obj(name):return json.loads(read(name))
    def lines(name):
        raw=read(name);return [json.loads(x) for x in raw.splitlines() if x.strip()] if raw.endswith(b'\n') else [json.loads(x) for x in raw.splitlines()[:-1] if x.strip()]
    signals=lines('evidence/paper_observation_five/signals.jsonl')
    results={};ledger=[]
    pool={'SHFE.AU':'SHFE.au2612','DCE.M':'DCE.m2701','CZCE.OI':'CZCE.OI701','DCE.Y':'DCE.y2701','DCE.P':'DCE.p2701'}
    for cs,symbol in pool.items():
        slug=cs.replace('.','_');base='evidence/paper_observation_five/products/'+slug
        meta=obj(base+'/market_metadata.local.json');cal=PaperCalendar(meta['dates'],meta['sessions'],cs)
        cp=obj('state/paper_observation_five/products/'+slug+'/core_checkpoint.json')
        fs=FactsState.from_dict(cp['runtime']['facts']['by_csymbol'][cs])
        cache_names=[n[len(prefix):] for n in index if n.startswith(prefix+'data/cache/paper_market/'+slug+'_') and n.endswith('/initial_m1.parquet')]
        union={};conflicts=set();manifests=[]
        def merge(record):
            if record.get('symbol')!=symbol:return
            t=pd.Timestamp(record['eob']).isoformat();value=tuple(float(record[k]) for k in ('open','high','low','close'))
            if t in union and union[t]!=value:conflicts.add(t)
            union.setdefault(t,value)
        for name in cache_names:
            blob=read(name);frame=pd.read_parquet(io.BytesIO(blob))
            for rec in frame.to_dict('records'):merge(rec)
            mp=name.rsplit('/',1)[0]+'/input_manifest.json'
            manifests.append(dict(cache=name,sha256=hashlib.sha256(blob).hexdigest(),manifest=obj(mp)))
        for record in lines(base+'/m1_observed.jsonl'):merge(record)
        lower,upper=(min(union),max(union)) if union else ('','')
        expected={t.isoformat() for t in cal.clock if union and lower<=t.isoformat()<=upper}
        missing=sorted(expected-set(union));observed=sorted([x for x in signals if x['csymbol']==cs],key=lambda x:x['bar_eob'])
        shadow={};counts=Counter();old_state=None;timing=[]
        for row in observed:
            f=row['facts'];td=f['trend_decision'];prior=td['prior_state'];actual=td['confirmed_state']
            if old_state is None:old_state=TrendState(trend_state=prior)
            elif old_state.trend_state!=prior:
                # Signals are an observation subset, not all warmup transitions.
                # Rejoin the recorded pre-state at a genuinely missing baseline
                # transition; never attribute that missing transition to a rule.
                counts['unobserved_baseline_transition_boundaries']+=1
                old_state=TrendState(trend_state=prior)
                if shadow:shadow['trend']=old_state.to_dict()
            facts=TrendFacts(bar_id=f['bar_id'],close=f['close'],high=f.get('high',f['close']),low=f.get('low',f['close']),
                close_side=f['trend_inputs']['close_side'],slope_state=f['slope_state'],top_divergence=f['top_div'],bottom_divergence=f['bottom_div'],
                **{k:v for k,v in f['trend_inputs'].items() if k not in ('close_side','h1_relative','h1_divergence')},
                h1_state=f['trend_inputs']['h1_relative'],h1_divergence=f['trend_inputs']['h1_divergence'])
            # Structural tombstones become visible only at their original broken bar.
            causal=FactsState(pivots=fs.pivots,divergences=fs.divergences,
                invalidated={k:v for k,v in fs.invalidated.items() if v['invalidated_at'].rsplit('|',1)[-1]<=row['bar_eob']})
            out=advance(shadow,causal,old_state,facts)
            old=step_trend(old_state,facts);old_state=old.state
            counts['evaluations']+=1;counts['old_'+actual]+=1;counts['shadow_'+out['decision']['confirmed_state']]+=1
            counts['old_recompute_action_mismatch']+=old.proposed_action!=td['proposed_action']
            counts['direction_disagreements']+=actual!=out['decision']['confirmed_state']
            counts['shadow_confirmations']+=out['decision']['confirmed']
            counts['shadow_direct_flips']+=out['decision']['confirmed'] and out['decision']['prior_state'] in ('bull','bear') and out['decision']['confirmed_state'] in ('bull','bear')
            entry=row.get('entry') or {};counts['actual_qualified']+=bool(entry.get('accepted'))
            if row.get('order_evaluated_at'):timing.append((datetime.fromisoformat(row['order_evaluated_at'])-datetime.fromisoformat(row['bar_eob'])).total_seconds())
            ledger.append(dict(csymbol=cs,bar_eob=row['bar_eob'],old=actual,shadow=out['decision']['confirmed_state'],
                shadow_votes=out['decision']['state']['establish_votes'],consumed=len(shadow['consumed']),actual_qualified=bool(entry.get('accepted')),
                shadow_path_qualification='NOT_EVALUATED',actual_historical_receipt_clock='UNKNOWN',
                comparison_scope='RECORDED_TREND_INPUTS;H1_RELATIVE_FROM_ORIGINAL_EPISODE;NOT_FULL_NEW_ENTRY_REPLAY'))
        results[cs]=dict(actual_symbol=symbol,cache_count=len(cache_names),cache_inputs=manifests,
            checkpoint_seed=cp.get('seed_cache'),union_minutes=len(union),missing_count=len(missing),missing_eob=missing,
            revised_minute_count=len(conflicts),revised_minute_keys=sorted(conflicts),
            cache_consumption_scope='ALL_SAVED_INITIAL_CACHES_INVENTORIED;CHECKPOINT_SEED_PROVEN;PER_RESTART_CONSUMPTION_NOT_FULLY_PERSISTED',
            historical_clock='UNKNOWN;CACHE_CREATION_NOT_HISTORICAL_AVAILABILITY',comparison=dict(counts),
            max_observed_evaluation_delay_seconds=max(timing,default=None),shadow_consumed=shadow.get('consumed',{}))
        if cs=='SHFE.AU':
            until='2026-11-10T21:15:00+08:00'
            results[cs]['old_subset_gap_resolution']={}
            for kind,pivot in [('top','2026-11-05T10:15:00+08:00'),('bottom','2026-11-06T21:45:00+08:00')]:
                required={t.isoformat() for t in cal.clock if pivot<t.isoformat()<=until}
                absent=sorted(required-set(union))
                results[cs]['old_subset_gap_resolution'][kind]=dict(expected=len(required),missing_count=len(absent),missing_eob=absent,
                    conclusion='ALL_SAVED_CACHE_UNION_ONLY;PER_RUN_ACTUAL_CONSUMPTION_AND_REAL_HISTORICAL_CLOCK_UNKNOWN')
    output.mkdir(parents=True,exist_ok=True)
    private=dict(products=results,ledger=ledger,freeze_errors=manifest['errors'],freeze_manifest_sha256=hashlib.sha256((root/'freeze_manifest.local.json').read_bytes()).hexdigest())
    (root/'historical_audit.local.json').write_text(json.dumps(private,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf8')
    report=dict(policy='DIVERGENCE_STRUCTURE_REPLACEMENT_SHADOW_V1_20261111',mode='SHADOW',
        products={k:{a:v[a] for a in ('cache_count','union_minutes','missing_count','revised_minute_count','comparison','max_observed_evaluation_delay_seconds')} for k,v in results.items()},
        gold_subset_gap_resolution=results['SHFE.AU']['old_subset_gap_resolution'],
        limitations=['Historical actual receive clocks UNKNOWN; cache union cannot establish past production completeness',
          'Missing baseline warmup transitions rejoin observed prior-state; counters explicitly list these boundaries',
          'Recorded-input trend comparison uses original episode-relative H1; not full new-policy path/order replay',
          'Direction coverage, confirmations and disagreements do not establish executable opportunities or net returns',
          'Missing independent causal admission evidence blocks ENFORCE; all final execution protections unchanged'],
        native_orders_sent=0,production_mutated=False)
    (output/'historical_comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--frozen',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();audit(a.frozen,a.output)
