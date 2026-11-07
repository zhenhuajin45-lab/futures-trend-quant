"""Independent core books, frozen M1, no SDK or orders. Friday account remains flat."""
import argparse,hashlib,json,csv,types,sys
from functools import lru_cache
from dataclasses import replace,asdict
from pathlib import Path
import pandas as pd
from futures_trend_quant.core.pipeline import step as core
from futures_trend_quant.core.entry import paths
from futures_trend_quant.core.entry.evidence import _latest_near_extreme_pattern
from futures_trend_quant.core.facts.indicators import calculate_indicators
from .episode import EpisodeBook
from tools.entry_compatibility.diagnose import read,write,csvwrite,START,END
from futures_trend_quant.core.models import Bar
from futures_trend_quant.live.paper_data import complete_bars


def clone(fn,**overrides):
    scope=dict(fn.__globals__);scope.update(overrides)
    return types.FunctionType(fn.__code__,scope,fn.__name__,fn.__defaults__,fn.__closure__)


def run(batch,out,product=None):
    if out.exists():raise ValueError('new output required')
    inputs=batch/'inputs';manifest=read(batch/'input_manifest.local.json')
    for r in manifest['files']:assert hashlib.sha256((inputs/r['path']).read_bytes()).hexdigest()==r['sha256']
    out.mkdir(parents=True);private=batch.parent/'ordered_b_state_replay_private';private.mkdir(exist_ok=True)
    # Research-process-only memoization of a pure numerical function. No host imports us.
    original_indicators=calculate_indicators
    cached=lru_cache(maxsize=32)(lambda bars: original_indicators(list(bars)))
    def indicators(bars):return cached(tuple(bars))
    for module in list(sys.modules.values()):
        if getattr(module,'__name__','').startswith('futures_trend_quant.') and getattr(module,'calculate_indicators',None) is original_indicators:
            module.calculate_indicators=indicators
    from futures_trend_quant.core.facts.swing import find_pivots as original_pivots
    pivot_cache=lru_cache(maxsize=32)(lambda bars,atr,kwargs:original_pivots(list(bars),atr,**dict(kwargs)))
    def pivots(bars,atr,**kwargs):return pivot_cache(tuple(bars),tuple(atr),tuple(sorted(kwargs.items())))
    for module in list(sys.modules.values()):
        if getattr(module,'__name__','').startswith('futures_trend_quant.') and getattr(module,'find_pivots',None) is original_pivots:
            module.find_pivots=pivots
    contract=Path(__file__).resolve().parents[2]/'reports/ordered_b_20261107/contract.json'
    write(out,'contract_freeze.json',dict(sha256=hashlib.sha256(contract.read_bytes()).hexdigest(),contract=read(contract),run_before_results=True))
    rules=read(inputs/'strategy.json')['rules'];rules=dict(rules,paper_permission_v1=True)
    logs=[json.loads(x) for x in (inputs/'signals.jsonl').read_text(encoding='utf-8').splitlines()]
    logs={(r['csymbol'],pd.Timestamp(r['bar_eob'])):r for r in logs if START<pd.Timestamp(r['bar_eob'])<=END}
    control_rows=[];ordered_rows=[];timeline=[];cases=[];parity=[]
    for cs in sorted({k[0] for k in logs}):
        if product is not None and cs!=product:continue
        folder=inputs/cs.replace('.','_');raw=pd.read_parquet(folder/'startup_cache.parquet').to_dict('records')
        raw += [r for line in (folder/'observed.jsonl').read_text(encoding='utf-8').splitlines() if START<pd.Timestamp((r:=json.loads(line))['eob'])<=END]
        minutes={}
        for r in raw:
            if pd.Timestamp(r['eob'])>END:continue
            b=Bar(r['symbol'],'60s',pd.Timestamp(r['bob']).to_pydatetime(),pd.Timestamp(r['eob']).to_pydatetime(),*[float(r[k]) for k in ('open','high','low','close')],float(r.get('volume',0)))
            if b.eob in minutes:assert minutes[b.eob]==b
            minutes[b.eob]=b
        m1=sorted(minutes.values(),key=lambda b:b.eob);m5=complete_bars(m1,5);m15=complete_bars(m1,15)
        control=core.StrategyRuntimeBook();ordered=None;episode=EpisodeBook();cursor=0;candidate=None
        def ordered_path_b(ctx,evidence,settings):
            if candidate is None:return None
            return paths.evaluate_path_b(ctx,replace(evidence,m5_pullback=candidate,m5_pattern=candidate),settings)
        evaluate=clone(paths.evaluate_entries,evaluate_path_b=ordered_path_b)
        ordered_core=clone(core.run_core_step,evaluate_entries=evaluate)
        for i,bar in enumerate(m15):
            if i<250:continue
            args=core.CoreStepInput(cs,bar.symbol,tuple(m15[max(0,i+1-1200):i+1]),tuple(b for b in m5 if b.eob<=bar.eob)[-1200:],tuple(b for b in m1 if b.eob<=bar.eob)[-1200:],rules)
            # Pure core state advances from original startup prefix, not final checkpoint.
            # Order feedback is identically empty: this incident is Friday forbidden.
            if pd.Timestamp(bar.eob)<=START-pd.Timedelta(minutes=45):
                core.run_core_step(args,control)
                while cursor<len(m5) and m5[cursor].eob<=bar.eob:cursor+=1
                if i%100==0:print(json.dumps(dict(product=cs,warmup=i,total=len(m15))),flush=True)
                continue
            if ordered is None:ordered=core.StrategyRuntimeBook.from_dict(control.to_dict())
            previous=ordered.trend_for(cs).trend_state
            while cursor<len(m5) and m5[cursor].eob<=bar.eob:
                prefix=m5[max(0,cursor+1-1200):cursor+1];ind=calculate_indicators(prefix)
                pattern=None if previous not in ('bull','bear') else _latest_near_extreme_pattern(prefix,ind,previous,rules['m5_flat_lookback'],rules['evidence_position_atr'])
                episode.step(m5[cursor],direction=previous,ema=ind.ema34[-1],atr=ind.atr14[-1],pattern=pattern,zone_atr=rules['pullback_min_atr'],max_bars=rules['m5_flat_lookback'])
                h=episode.history[-1] if episode.history and episode.history[-1]['at']==m5[cursor].eob.isoformat() else None
                if h:timeline.append(dict(csymbol=cs,**h))
                cursor+=1
            active=episode.active;candidate=None
            if active and active['confirmation']:
                confirm=active['confirmation']['eob'];index=next((j for j,b in enumerate(args.m5_bars) if b.eob.isoformat()==confirm),None)
                if index is not None:candidate=episode.evidence(index)
            a=core.run_core_step(args,control);b=ordered_core(args,ordered)
            if b.trend.confirmed_state!=previous:episode.end('AUTHORIZATION_CHANGED_AT_M15',bar.eob.isoformat())
            if START<pd.Timestamp(bar.eob)<=END:
                log=logs[(cs,pd.Timestamp(bar.eob))]
                same=a.trend.confirmed_state==log['facts']['trade_permission']['background'] and (None if a.entry is None else a.entry.path)==(log.get('entry') or {}).get('path')
                parity.append(dict(csymbol=cs,eob=bar.eob.isoformat(),logged_background=log['facts']['trade_permission']['background'],replayed_background=a.trend.confirmed_state,path_equal=(None if a.entry is None else a.entry.path)==(log.get('entry') or {}).get('path'),parity=same))
                for mode,result,dest in [('CONTROL',a,control_rows),('ORDERED',b,ordered_rows)]:
                    e=result.entry;dest.append(dict(csymbol=cs,eob=bar.eob.isoformat(),mode=mode,background=result.trend.confirmed_state,entry=None if e is None else asdict(e),permission=None if result.permission is None else asdict(result.permission),anchor=None if result.anchor is None else asdict(result.anchor),clock=result.evidence_clock,friday_calendar='BLOCK',risk_quote_account='NOT_EVALUATED_CALENDAR_BLOCK',model_order=False,model_fill=False))
                selected=(log.get('entry') or {}).get('path')
                if selected in ('B','C'):
                    # Entire prefix evidence is available; no last-pair-only association.
                    past=[h for h in episode.history if h['at']<=bar.eob.isoformat()]
                    cases.append(dict(csymbol=cs,eob=bar.eob.isoformat(),original_path=selected,classification='合法前置回调_最近摘要覆盖' if active and active['confirmation'] else '确认后破坏或过程结束' if episode.ended else '缺少合法前置关系',classification_scope='NEW_ORDERED_CONTRACT_NOT_RETROACTIVE_BASELINE_ERROR',active=active,ended=episode.ended[-4:],timeline_rows=len(past)))
            if i%200==0:print(json.dumps(dict(product=cs,processed=i,total=len(m15))),flush=True)
        write(private,cs.replace('.','_')+'_control.json',control.to_dict());write(private,cs.replace('.','_')+'_ordered.json',ordered.to_dict());write(private,cs.replace('.','_')+'_episode.json',episode.to_dict())
    csvwrite(out,'state_trace_control.csv',control_rows);csvwrite(out,'state_trace_ordered.csv',ordered_rows)
    csvwrite(out,'m5_process_timeline.csv',timeline);csvwrite(out,'case_classification.csv',cases);csvwrite(out,'baseline_reproduction.csv',parity)
    deltas=[]
    for a,b in zip(control_rows,ordered_rows):
        if a['entry']!=b['entry']:deltas.append(dict(csymbol=a['csymbol'],eob=a['eob'],old=a['entry'],ordered=b['entry'],account_feedback='NONE_FRIDAY_NO_ORDERS'))
    csvwrite(out,'candidate_deltas.csv',deltas)
    write(out,'account_comparison.json',dict(scope='FULL_CLOSED_BAR_CORE_PREFIX_REPLAY_NOT_EXECUTION_ARRIVAL_REPLAY',frames=len(parity),baseline_parity=sum(r['parity'] for r in parity),deltas=len(deltas),model_orders=0,model_fills=0,account_feedback='NO_DIFFERENCE_FRIDAY_FORBIDDEN',stop_executable_price='NOT_EVALUATED_NO_CALENDAR_ELIGIBLE_ORDER',profitability='NOT_EVALUATED',hist_policy='UNCHANGED',independent_sample_consumed=False))
    print(json.dumps(dict(frames=len(parity),parity=sum(r['parity'] for r in parity),deltas=len(deltas))),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--batch',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--product');a=p.parse_args();run(a.batch,a.output,a.product)
