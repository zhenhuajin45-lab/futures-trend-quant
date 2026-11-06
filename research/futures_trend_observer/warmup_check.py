"""Existing 1200-M15 indicator window vs available longer prefix; no new model."""
from datetime import datetime,timezone
from pathlib import Path
import argparse
from tools.futures_trend_review import read,write_csv,load_minutes,complete_bars,POOL
from futures_trend_quant.core.facts.indicators import calculate_indicators
from futures_trend_quant.live.trend_observation import build


def check(source,output):
    states=read(source/'all_states.local.json');results=[]
    focus={'2026-11-05T22:45:00+08:00','2026-11-06T00:00:00+08:00','2026-11-06T00:15:00+08:00','2026-11-06T01:00:00+08:00'}
    for cs in POOL:
        series=complete_bars(load_minutes(source,cs)[0],15);cases=[r for r in states if r['csymbol']==cs]
        selected={cases[0]['bar_eob'],cases[-1]['bar_eob']} | (focus if cs=='SHFE.AU' else set())
        for row in cases:
            if row['bar_eob'] not in selected:continue
            prefix=[b for b in series if b.eob<=datetime.fromisoformat(row['bar_eob'])]
            assert len(prefix)>=1200,'approved warmup unavailable'
            window=calculate_indicators(prefix[-1200:]);full=calculate_indicators(prefix)
            f=dict(symbol=row['actual_symbol'],eob=row['bar_eob'],bar_id=row['source_bar_ids'][0],
                h1_absolute=row['h1_structure_class'],slope_state=window.slope_state[-1],
                trend_inputs=dict(close_side='above' if row['close']>row['ema34'] else 'below' if row['close']<row['ema34'] else 'equal'),
                trade_permission=row['production_permission'],trend_decision=row['trend_decision'],top_div=False,bottom_div=False)
            now=datetime.now(timezone.utc).isoformat();context=dict(background=row['production_m15_background'],closed=True,complete=True,computed_at=now)
            a=build(f,hist=window.hist[-1],previous_hist=window.hist[-2],**context)
            b=build(f,hist=full.hist[-1],previous_hist=full.hist[-2],**context)
            item=dict(csymbol=cs,eob=row['bar_eob'],approved_window=1200,available_prefix=len(prefix),
                ema34_absolute_difference=abs(window.ema34[-1]-full.ema34[-1]),
                atr14_absolute_difference=abs(window.atr14[-1]-full.atr14[-1]),
                histogram_absolute_difference=abs(window.hist[-1]-full.hist[-1]),
                slope_state_equal=window.slope_state[-1]==full.slope_state[-1],
                phase_equals_saved_core_explanation=a['phase']==row['phase'],phase_window_equal=a['phase']==b['phase'],
                scope='INDICATOR_LEFT_EDGE_ONLY;PERSISTED_S0_EPISODES_NOT_RESET')
            assert item['slope_state_equal'] and item['phase_equals_saved_core_explanation'] and item['phase_window_equal'],item
            results.append(item)
    write_csv(output,results);print({'checked':len(results),'all_phase_matches':True})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();check(a.source,a.output)
