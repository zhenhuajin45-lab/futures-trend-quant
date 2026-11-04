"""Derived per-actual-leg gap table; never requests or labels scoring prices."""
from pathlib import Path
from bisect import bisect_left
from collections import defaultdict
import sys
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,digest
from tools.first_entry_v71.clock import dates,calendar_clock,anchor_rows,timestamp,DAY_SESSIONS,NIGHT_END


def main():
    source=ROOT/'data/cache/v71_source_inventory/metadata.json';meta=read(source)
    prior_probe=ROOT/'data/cache/v7_edb_source_probe/receipt.json'
    prior=read(prior_probe) if prior_probe.is_file() else []
    rows=[];reg=read(ROOT/'tools/first_entry_v7/registration.json')
    calendar=dict(coverage_start='2025-06-01',coverage_end='2026-05-06',products={})
    for cs in reg['new_sample']['universe']:
        trade_days=meta['calendars'][cs.split('.')[0]]
        open_days=set(trade_days);previous=None;records=[]
        for day in dates(calendar['coverage_start'],calendar['coverage_end']):
            opened=day in open_days
            night=previous if opened and previous and (pd.Timestamp(day)-pd.Timestamp(previous)).days<=3 and cs in NIGHT_END else None
            record=dict(date=day,is_open=opened,night_start_date=night)
            if opened and cs in NIGHT_END and night is None:record['night_closed_reason']='VERIFIED_HOLIDAY_REOPEN'
            records.append(record)
            if opened:previous=day
        # This is an explicitly unverified session proxy for estimating gaps only.
        # It is NEVER used to freeze the real admission lock or run the strategy.
        calendar['products'][cs]=dict(days=records,day_sessions=[list(x) for x in DAY_SESSIONS],night_end=NIGHT_END.get(cs))
        clock=calendar_clock(calendar,cs);anchors=anchor_rows(clock)
        by_day=defaultdict(list);day_by_eob={}
        for anchor in anchors:
            t=timestamp(anchor['eob']);by_day[anchor['trading_date']].append(t);day_by_eob[t]=anchor['trading_date']
        mapping={r['trade_date']:r['symbol'] for r in meta['mapping'][cs]}
        selected={}
        for index,day in enumerate(trade_days):
            if '2026-01-05'<=day<='2026-04-24':
                previous=trade_days[index-1] if index else None
                if previous not in mapping:raise ValueError('missing preceding-day mapping: '+cs+' '+str(previous))
                selected[day]=mapping[previous]
        listing={r['symbol']:r for r in meta['metadata'].get(cs,[]) if isinstance(r,dict) and 'symbol' in r}
        all_eobs=[timestamp(a['eob']) for a in anchors]
        for symbol in sorted(set(selected.values())):
            active_days=sorted(d for d,s in selected.items() if s==symbol)
            first=by_day[active_days[0]][0]
            required=[t for t in all_eobs if t<first][-1200:]
            missing_days=sorted({day_by_eob[t] for t in required})
            probes=[r for r in meta['probes'] if r['symbol']==symbol]
            received=sum(r.get('rows',0) for r in probes)
            free_upper=sum(len(by_day[d]) for d in trade_days if '2025-11-04'<=d<active_days[0])
            first_needed=(required[0]-pd.Timedelta(minutes=14)).isoformat() if len(required)==1200 else None
            rows.append(dict(csymbol=cs,exchange=cs.split('.')[0],actual_contract=symbol,
                listed_at=listing.get(symbol,{}).get('listed_date','UNKNOWN'),delisted_at=listing.get(symbol,{}).get('delisted_date','UNKNOWN'),
                first_selected_trade_date=active_days[0],last_selected_trade_date=active_days[-1],selected_trade_days=len(active_days),
                mapping_proxy='PREVIOUS_COMPLETED_TRADING_DAY_GM_DOMINANT; PUBLICATION_TIME_UNKNOWN',
                mapping_source_sha256=digest(source),first_expected_scoring_eob_proxy=first.isoformat(),
                last_label_cap='2026-05-06T15:00:00+08:00',
                historical_session_basis='UNVERIFIED_PROXY; NOT_ADMISSION_DENOMINATOR',
                expected_scoring_m1_proxy=sum(len(by_day[d])*15 for d in active_days),
                expected_warmup_m1_proxy=18000,clock_only_probe_minutes_received=received,
                scoring_ohlcv_m1_received=sum(r.get('rows',0) for r in prior if r['symbol']==symbol and r.get('http_status')==200),
                complete_scoring_m15_received=0,qualified_complete_warmup_m15=0,required_warmup_m15=1200,
                missing_warmup_m15=1200,earliest_required_warmup_eob_proxy=first_needed,
                missing_warmup_trade_dates_proxy=missing_days,
                free_year_full_day_m15_upper_bound_before_first_day=free_upper,
                free_year_cannot_alone_cover_warmup=free_upper<1200,
                status='READY_FOR_DATA',historical_arrival_at='UNKNOWN',historical_mapping_known_at='UNKNOWN'))
    output=ROOT/'reports/v71_actual_leg_data_gaps.json'
    write(output,dict(status='READY_FOR_DATA',actual_legs=len(rows),products=12,
        no_real_price_labels_read=True,no_real_strategy_replay=True,
        source_sha256=digest(source),expected_scoring_trade_days_per_product=73,
        calendar_notice_and_historical_session_qualification='PENDING; GM date metadata alone insufficient',
        probe_summary=dict(september_http_403=sum(r.get('http_status')==403 for r in meta['probes']),
            december_http_200=sum(r.get('http_status')==200 for r in meta['probes']),
            clock_only_minutes_received=sum(r.get('rows',0) for r in meta['probes']),
            existing_january_ohlcv_probe_minutes=sum(r.get('rows',0) for r in prior)),
        bigquant_environment_keys_present=meta.get('existing_bigquant_environment_present',False),
        next_material='Earlier authorized actual-contract M1 covering each listed warmup gap, full scoring/tail M1, '
            'historical official session/holiday evidence and frozen source policy. No purchase or login attempted.',
        rows=rows))
    flat=[dict(r,missing_warmup_trade_dates_proxy='|'.join(r['missing_warmup_trade_dates_proxy'])) for r in rows]
    pd.DataFrame(flat).to_csv(ROOT/'reports/v71_actual_leg_data_gaps.csv',index=False)
    print('ACTUAL_LEG_GAPS',len(rows),'READY_FOR_DATA',flush=True)


if __name__=='__main__':main()
