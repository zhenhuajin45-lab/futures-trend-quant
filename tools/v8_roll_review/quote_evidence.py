"""Late old-window entry-side quote evidence; not a certified execution model."""
from pathlib import Path
import os,json,hashlib
import pandas as pd
from backtest.forward_v3 import read,write

ROOT=Path(__file__).resolve().parents[2]


def assess_quote(direction,lots,raw_open,tick,quote):
    sign=1 if direction=='bull' else -1
    side='ask' if sign==1 else 'bid';price=float(quote[side+'_p']);size=float(quote[side+'_v'])
    if price<=0 or size<=0:return dict(status='NO_VALID_OPPOSITE_QUOTE',visible_quantity_sufficient=False)
    adverse=sign*(price-raw_open)/tick
    return dict(status='VISIBLE_TOP_SUFFICIENT' if size>=lots else 'INSUFFICIENT_TOP_DEPTH',
        visible_quantity_sufficient=size>=lots,visible_top_lots=size,adverse_ticks_to_raw_open=float(adverse),
        within_1_tick=bool(adverse<=1+1e-7),within_2_ticks=bool(adverse<=2+1e-7))


def main():
    out=ROOT/'data/cache/v8_late_entry_quotes';out.mkdir(parents=True,exist_ok=False)
    frames=[]
    for group in ('S0','ORDINARY'):
        for suffix in ('trades','opened'):
            f=pd.read_csv(ROOT/'reports'/('v8_REFERENCE_1T_'+group+'_'+suffix+'.csv'))
            f=f[pd.to_datetime(f.entry_bob,utc=True)>=pd.Timestamp('2026-10-28T00:00:00+08:00')]
            frames.append(f[['group','symbol','csymbol','entry_bob','entry_price','lots','direction']])
    fills=pd.concat(frames,ignore_index=True);specs=read(ROOT/'config/fees.json')['products'];records=[];responses={}
    from gm.api import history,set_token
    set_token(os.environ['GM_TOKEN'])
    for i,r in enumerate(fills.to_dict('records')):
        key=(r['symbol'],r['entry_bob']);begin=pd.Timestamp(r['entry_bob'])
        if key not in responses:
            try:
                ticks=history(symbol=r['symbol'],frequency='tick',start_time=begin.to_pydatetime(),
                    end_time=(begin+pd.Timedelta(seconds=5)).to_pydatetime(),df=True)
                path=out/(str(len(responses))+'_'+r['symbol'].replace('.','_')+'.pkl');ticks.to_pickle(path)
                responses[key]=(ticks,hashlib.sha256(path.read_bytes()).hexdigest(),None)
            except Exception as exc:responses[key]=(None,None,type(exc).__name__)
        ticks,sha,error=responses[key]
        row={k:r[k] for k in ('group','symbol','entry_bob','lots','direction')};row.update(source_sha256=sha)
        if ticks is None:row.update(status='SOURCE_UNAVAILABLE',error_type=error)
        else:
            timestamps=pd.to_datetime(ticks.created_at,utc=True) if len(ticks) else pd.DatetimeIndex([])
            ordered=ticks.assign(_t=timestamps).sort_values('_t') if len(ticks) else ticks
            ordered=ordered[ordered._t>=begin] if len(ordered) else ordered
            if ordered.empty:row.update(status='NO_QUOTE_IN_FIXED_5SEC_WINDOW')
            else:
                first=ordered.iloc[0];quote=first.quotes[0] if len(first.quotes) else None
                if quote is None:row.update(status='NO_QUOTE_FIELDS')
                else:
                    tick=specs[r['csymbol'].split('.')[1]]['price_tick'];sign=1 if r['direction']=='bull' else -1
                    raw_open=r['entry_price']-sign*tick
                    row.update(assess_quote(r['direction'],r['lots'],raw_open,tick,quote),
                        first_quote_delay_seconds=(first._t-begin).total_seconds(),
                        bid_ask_spread_ticks=(quote['ask_p']-quote['bid_p'])/tick if quote['ask_p']>0 and quote['bid_p']>0 else None)
        records.append(row)
        if (i+1)%10==0:print('OLD_WINDOW_ENTRY_QUOTES',i+1,'/',len(fills),flush=True)
    pd.DataFrame(records).to_csv(ROOT/'reports/v8_roll_entry_quote_evidence.csv',index=False)
    frame=pd.DataFrame(records);good=frame.get('visible_quantity_sufficient',pd.Series(False,index=frame.index)).fillna(False).astype(bool)
    write(ROOT/'reports/v8_roll_quote_summary.json',dict(scope='ALL_CORRECTED_MAIN_ACCOUNT_ENTRIES_2026_10_28_TO_11_02',
        fills=len(fills),unique_requests=len(responses),status_counts=frame.status.value_counts().to_dict(),
        sufficient_visible_top_observations=int(good.sum()),
        sufficient_and_within_1_tick=int((good & frame.get('within_1_tick',pd.Series(False,index=frame.index)).fillna(False).astype(bool)).sum()),
        sufficient_and_within_2_ticks=int((good & frame.get('within_2_ticks',pd.Series(False,index=frame.index)).fillna(False).astype(bool)).sum()),
        data_source='GM_AUTHORIZED_HISTORICAL_TICK',quote_levels=1,quote_execution_certified=False,
        limitations=['FIRST_QUOTE_AT_OR_AFTER_MODEL_OPEN_WITHIN_5_SECONDS','ENTRY_SIDE_ONLY_NOT_STOP_OR_EXIT_EXECUTION',
            'DISPLAYED_SIZE_NOT_GUARANTEED_FILL','DEEPER_BOOK_AND_IMPACT_UNKNOWN_WHEN_TOP_TOO_SMALL',
            'DOES_NOT_VALIDATE_2025_HISTORY_FEES_OR_QUOTES','LOCAL_HISTORICAL_ARRIVAL_TIME_UNKNOWN'],orders=False))
    print('V8_OLD_WINDOW_QUOTE_EVIDENCE_COMPLETE',flush=True)


if __name__=='__main__':main()
