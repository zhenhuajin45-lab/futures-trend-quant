"""Production ordered book + selector on frozen, already observed closed facts.

Not a new independent account backtest: unchanged background/H1 facts are recorded
inputs, price/risk follow-through remains separately qualified. No broker imports.
"""
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
import json,argparse,hashlib
import pandas as pd
from futures_trend_quant.core.models import Bar
from futures_trend_quant.core.entry.ordered_b import OrderedBBook
from futures_trend_quant.core.entry.evidence import build_entry_evidence
from futures_trend_quant.core.entry.gate import evaluate_momentum_gate
from futures_trend_quant.core.entry.models import EntryContext
from futures_trend_quant.core.entry.paths import evaluate_entries
from futures_trend_quant.core.entry.v5 import select
from futures_trend_quant.core.pipeline.step import _entry_settings
from futures_trend_quant.live.paper_calendar import PaperCalendar
from futures_trend_quant.live.paper_data import complete_bars
from tools.entry_compatibility.diagnose import read,write,csvwrite,START,END


def run(batch,out):
    if out.exists():raise ValueError('new replay directory required')
    inputs=batch/'inputs';manifest=read(batch/'input_manifest.local.json')
    for r in manifest['files']:assert hashlib.sha256((inputs/r['path']).read_bytes()).hexdigest()==r['sha256']
    out.mkdir(parents=True);rules=read(inputs/'strategy.json')['rules'];settings=_entry_settings(rules)
    records=[json.loads(x) for x in (inputs/'signals.jsonl').read_text(encoding='utf-8').splitlines()]
    records=[r for r in records if START<pd.Timestamp(r['bar_eob'])<=END]
    rows=[];audits=[];processes=[];clocks={};baseline_matches=0
    for cs in sorted({r['csymbol'] for r in records}):
        scope=sorted([r for r in records if r['csymbol']==cs],key=lambda r:r['bar_eob']);key=cs.replace('.','_');meta=read(inputs/key/'metadata.json')
        calendar=PaperCalendar(meta['dates'],meta['sessions'],cs);book=OrderedBBook(last_eob=START.isoformat())
        raw=pd.read_parquet(inputs/key/'startup_cache.parquet').to_dict('records')
        raw += [r for line in (inputs/key/'observed.jsonl').read_text(encoding='utf-8').splitlines() if START<pd.Timestamp((r:=json.loads(line))['eob'])<=END]
        minute={}
        for r in raw:
            end=pd.Timestamp(r['eob']).to_pydatetime()
            if end>END:continue
            b=Bar(r['symbol'],'60s',pd.Timestamp(r['bob']).to_pydatetime(),end,*[float(r[k]) for k in ('open','high','low','close')],float(r.get('volume',0)))
            if end in minute:assert minute[end]==b
            minute[end]=b
        m1=sorted(minute.values(),key=lambda b:b.eob);m5=complete_bars(m1,5);m15=complete_bars(m1,15)
        prior=scope[0]['facts']['trend_decision']['prior_state']
        for r in scope:
            end=pd.Timestamp(r['bar_eob']);f=r['facts'];direction=f['trade_permission']['background']
            a=[b for b in m1 if b.eob<=end][-1200:];b=[x for x in m5 if x.eob<=end][-1200:];c=[x for x in m15 if x.eob<=end][-1200:]
            sessions={x.identity:calendar.section_for_bar(x) for x in b if x.eob>START}
            book.advance(b,prior,sessions,rules,None)
            if direction!=prior:book.end('M15_AUTHORIZATION_CHANGED',end.isoformat())
            prior=direction
            if direction not in ('bull','bear'):
                rows.append(dict(csymbol=cs,symbol=meta['symbol'],eob=r['bar_eob'],selected=None,accepted=False,reason='NO_DIRECTION_AUTHORIZATION',calendar_block=calendar.entry_block(end),account='NOT_EVALUATED',order=False));continue
            evidence=build_entry_evidence(b,a,direction,div_mode=rules['div_mode'],m5_lookback=rules['m5_flat_lookback'],flat_ratio=rules['m5_flat_slope_ratio'],position_atr=rules['evidence_position_atr'],pullback_atr=rules['pullback_min_atr'],consolidation_bars=rules['consolidation_bars'],consolidation_range_atr=rules['consolidation_range_atr'],m1_lookback=rules['m1_evidence_lookback'])
            gate=evaluate_momentum_gate(c,direction,enabled=rules['gate_enabled'],distance_atr=rules['gate_distance_atr'],shrink_bars=rules['gate_hist_shrink_bars'])
            ctx=EntryContext(direction,True,f['h1_absolute'],f['slope_state'],f['top_div'] if direction=='bull' else f['bottom_div'],gate.is_open,current_price=f['close'],atr14=f['atr14'])
            old=evaluate_entries(ctx,evidence,settings);assert old.path==(r.get('entry') or {}).get('path');baseline_matches+=1
            pf=dict(close_side=f['trend_inputs']['close_side'],slope=f['slope_state'],hist=r['trend_observation']['momentum_hist'],opposite_divergence=ctx.m15_reverse_divergence,closed_h1_known=f['h1_absolute'] is not None,calendar_qualified=bool(f['h1_source']['calendar_qualified']),previous_weak=0,changed=False,eob=r['bar_eob'])
            selected,p,clock,case=select(ctx,evidence,settings,book.candidate(b),facts=pf,clocks=clocks,csymbol=cs,eob=end.to_pydatetime())
            rows.append(dict(csymbol=cs,symbol=meta['symbol'],eob=r['bar_eob'],selected=selected.path,accepted=selected.accepted,reason=selected.reason,evidence_id=selected.evidence_id,clock=clock,calendar_block=calendar.entry_block(end),account='NOT_EVALUATED',order=False))
            audits.extend(dict(csymbol=cs,eob=r['bar_eob'],**x) for x in case)
        processes.append(dict(csymbol=cs,state=book.to_dict()))
    csvwrite(out,'case_decisions.csv',rows);csvwrite(out,'per_path_audit.csv',audits);write(out,'processes.json',processes)
    summary=dict(scope='SEEN_RECORDED_BACKGROUND_FACTS_PLUS_FORMAL_M5_PROCESS_AND_SELECTOR_NOT_FULL_ACCOUNT_BACKTEST',frames=len(rows),baseline_directional_path_matches=baseline_matches,strategy_qualified=sum(r['accepted'] for r in rows),selected_paths=[dict(csymbol=r['csymbol'],eob=r['eob'],path=r['selected']) for r in rows if r['accepted']],orders=0,friday_policy_preserved=True,production_calls_used=['OrderedBBook.advance','evaluate_entries','v5.select'],research_function_replacement=False,cost_profit_claim=False)
    write(out,'summary.json',summary);print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--batch',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();run(a.batch,a.output)
