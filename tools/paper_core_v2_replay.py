"""Fixed private gold replay, no network/SDK/orders/PnL; sanitized derived rows only."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
from dataclasses import asdict
import argparse,hashlib,json,csv,time
import pandas as pd
from futures_trend_quant.core.models import Bar
from futures_trend_quant.core.pipeline import CoreStepInput,StrategyRuntimeBook,run_core_step
from futures_trend_quant.config import load_config
from futures_trend_quant.live.paper_data import complete_bars
from futures_trend_quant.live.paper_config import frozen_core_digest
from futures_trend_quant.core.facts.indicators import calculate_indicators
from futures_trend_quant.core.facts.pipeline import aggregate_m15_to_h1_with_coverage

TZ=timezone(timedelta(hours=8))
START=datetime(2026,11,5,21,tzinfo=TZ);END=datetime(2026,11,6,2,30,tzinfo=TZ)


def run(client,output,permission=True):
    root=client/'_strategy_runtime';output.mkdir(parents=True,exist_ok=True)
    cache=root/'data/cache/paper_market/SHFE_AU_20261105T120931563192'
    manifest=json.loads((cache/'input_manifest.json').read_text(encoding='utf8'))
    seed=cache/'initial_m1.parquet';assert hashlib.sha256(seed.read_bytes()).hexdigest()==manifest['sha256']
    raw=(root/'evidence/paper_observation_five/products/SHFE_AU/m1_observed.jsonl').read_bytes()
    rows=pd.read_parquet(seed).to_dict('records')
    for line in raw.decode('utf8').splitlines():
        try:r=json.loads(line)
        except ValueError:continue
        if pd.Timestamp(r['eob']).to_pydatetime()<=END:rows.append(r)
    minutes={}
    for row in rows:
        bob=pd.Timestamp(row['bob']).to_pydatetime();eob=pd.Timestamp(row['eob']).to_pydatetime()
        if eob>END:continue
        b=Bar(row['symbol'],'60s',bob,eob,*[float(row[k]) for k in ('open','high','low','close')],
              float(row.get('volume',0)),float(row.get('amount',0)),float(row.get('position',0)))
        if b.identity in minutes:
            old=minutes[b.identity]
            assert (old.open,old.high,old.low,old.close)==(b.open,b.high,b.low,b.close),'conflicting original OHLC'
        minutes[b.identity]=b
    bars=sorted(minutes.values(),key=lambda b:b.eob);m15=complete_bars(bars,15);m5=complete_bars(bars,5)
    rules=dict(load_config(root/'config/strategy.json').raw['rules'])
    if permission:rules['paper_permission_v1']=True
    runtime=StrategyRuntimeBook();report=[];started=time.monotonic()
    code_digest=frozen_core_digest(Path(__file__).resolve().parents[1])
    for index,bar in enumerate(m15):
        if index<250:continue
        history=m15[max(0,index+1-1200):index+1]
        result=run_core_step(CoreStepInput('SHFE.AU',bar.symbol,tuple(history),
            tuple(b for b in m5 if b.eob<=bar.eob)[-1200:],tuple(b for b in bars if b.eob<=bar.eob)[-1200:],rules),runtime)
        if START<bar.eob<=END:
            a=result.anchor;e=result.entry
            report.append(dict(eob=bar.eob.isoformat(),close=bar.close,ema34=result.facts_row['ema34'],atr14=result.facts_row['atr14'],
                background=result.trend.confirmed_state,proposal=result.trend.proposed_action,trend_reason=result.trend.reason_code,
                h1=result.analysis.h1_absolute,h1_source=result.analysis.h1_source,
                top_div=result.facts.top_divergence,bottom_div=result.facts.bottom_divergence,
                permission=None if result.permission is None else asdict(result.permission),
                entry=None if e is None else asdict(e),anchor=None if a is None else asdict(a),
                evidence_clock=result.evidence_clock,trend_inputs=result.facts_row['trend_inputs'],
                divergence_state={k:dict(events=[asdict(d) for d in state.divergences[-2:]],invalidated=state.invalidated)
                    for k,state in runtime.facts.by_csymbol.items()}))
        if index%100==0:print(json.dumps(dict(processed=index,total=len(m15),elapsed_seconds=round(time.monotonic()-started))),flush=True)
    sources=dict(seed_sha256=manifest['sha256'],observed_prefix_bytes=len(raw),observed_prefix_sha256=hashlib.sha256(raw).hexdigest(),
                 warmup_normalizations=manifest.get('zero_trade_normalization'),originals_remain_private=True)
    # Same-indicator figures use only these closed sources, never chart-app MAs.
    chart=[]
    for freq,series in [('M15',m15),('SESSION_H1',[x.bar for x in aggregate_m15_to_h1_with_coverage(m15) if x.closed and x.complete])]:
        indicators=calculate_indicators(series)
        for i,b in enumerate(series):
            if datetime(2026,11,4,tzinfo=TZ)<=b.eob<=END:
                chart.append(dict(frequency=freq,eob=b.eob.isoformat(),open=b.open,high=b.high,low=b.low,close=b.close,
                    ema34=indicators.ema34[i],dif=indicators.dif[i],dea=indicators.dea[i],hist=indicators.hist[i]))
    result=dict(scope='FIXED_INCIDENT_PREFIX_REPLAY_NO_ORDERS_NO_PROFIT_SCORE',policy='V2_PERMISSION_V1' if permission else 'V2_FACTS_ONLY',
                core_sha256_lf=code_digest,
                source=sources,m1_rows=len(bars),m15_rows=len(m15),night_rows=len(report),rows=report,
                execution_recovery_passed=False,independent_sample_consumed=False,review='SINGLE_AGENT_SELF_CHECK')
    (output/'gold_replay.local.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf8')
    (output/'chart_sources.local.json').write_text(json.dumps(chart,ensure_ascii=False,indent=2,default=str),encoding='utf8')
    (output/'runtime_final.local.json').write_text(json.dumps(runtime.to_dict(),ensure_ascii=False),encoding='utf8')
    print(json.dumps(dict(output=str(output),night_rows=len(report),elapsed_seconds=round(time.monotonic()-started))),flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--client',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--facts-only',action='store_true');a=p.parse_args();run(a.client,a.output,not a.facts_only)
