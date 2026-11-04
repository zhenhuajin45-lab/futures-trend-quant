"""One data-access inventory and precise procurement scope; never probes denied bars."""
from pathlib import Path
import os,json,importlib.util,hashlib
import pandas as pd
from backtest.forward_v3 import read,write,digest

ROOT=Path(__file__).resolve().parents[2]
SUFFIX={'SHFE':'SHF','DCE':'DCE','CZCE':'CZC','GFEX':'GFE'}


def main():
    out=ROOT/'data/cache/v9_data_request';out.mkdir(parents=True,exist_ok=False)
    target=ROOT/'reports/v9_holding';target.mkdir(parents=True,exist_ok=True)
    from gm.api import set_token,get_trading_dates,fut_get_continuous_contracts
    set_token(os.environ['GM_TOKEN']);raw=dict(calendars={},mapping={});rows=[]
    for exchange in SUFFIX:
        raw['calendars'][exchange]=get_trading_dates(exchange,'2025-03-03','2026-01-12')
    score=[str(x)[:10] for x in raw['calendars']['SHFE'] if '2025-09-01'<=str(x)[:10]<='2025-12-31']
    assert len(score)==82
    for exchange in SUFFIX:
        assert [str(x)[:10] for x in raw['calendars'][exchange] if '2025-09-01'<=str(x)[:10]<='2025-12-31']==score
    dates=[str(x)[:10] for x in raw['calendars']['SHFE']]
    for item in read(ROOT/'config/strategy.json')['universe']:
        cs=item['csymbol'];mapping=fut_get_continuous_contracts(cs,'2025-09-01','2025-12-31');raw['mapping'][cs]=mapping
        for symbol in sorted({r['symbol'] for r in mapping}):
            days=sorted(str(r['trade_date'])[:10] for r in mapping if r['symbol']==symbol)
            first,last=days[0],days[-1];i=dates.index(first);j=dates.index(last)
            exchange,code=symbol.split('.')
            rows.append(dict(csymbol=cs,actual_gm=symbol,actual_bigquant=code+'.'+SUFFIX[exchange],
                first_selected_day=first,last_selected_day=last,selected_trading_days=len(days),
                raw_m1_request_begin='2025-03-03T00:00:00+08:00',raw_m1_request_end='2025-12-31T15:00:00+08:00',
                quote_request_begin=dates[i-1]+'T20:59:55+08:00',
                quote_request_end=min('2025-12-31',dates[j+1])+'T15:00:05+08:00',
                minimum_same_leg_complete_m15_before_first_selected=1200,
                mapping_known_at='UNKNOWN_NOT_QUALIFIED',raw_m1_qualified=False,quote_qualified=False,
                exclusion_reason='NO_AUTHORIZED_READABLE_FULL_WINDOW_PACKAGE'))
        print('V9_REQUEST_SCOPE_METADATA',cs,flush=True)
    metadata=out/'metadata.json';metadata.write_text(json.dumps(raw,default=str,ensure_ascii=False),encoding='utf8')
    pd.DataFrame(rows).to_csv(target/'independent_contract_request.csv',index=False)
    products=[]
    for cs in raw['mapping']:
        products.append(dict(csymbol=cs,required_scoring_days=82,admitted=False,
            reason='NO_COMPLETE_AUTHORIZED_M1_PREWARM_AND_L1_PACKAGE',mapping_timestamp_known=False))
    pd.DataFrame(products).to_csv(target/'independent_product_status.csv',index=False)
    names=('BIGQUANT_TOKEN','BIGQUANT_API_KEY','BQ_API_KEY','BIGQUANT_USERNAME','BIGQUANT_PASSWORD')
    write(target/'data_access_attempt.json',dict(status='ONE_ATTEMPT_CLOSED_NO_EXISTING_AUTHORIZATION',
        auth_environment_present={k:bool(os.environ.get(k)) for k in names},dai_installed=importlib.util.find_spec('dai') is not None,
        qualified_products=0,raw_export_completed=False,independent_outcomes_opened=False,
        actual_contracts=len(rows),score_days=82,metadata_sha256=digest(metadata),
        denied_gm_edb_requests_repeated=False,new_vendor_account_created=False,purchase_performed=False,
        required_tables=['cn_future_bar1m_c','cn_future_level1_snapshot','cn_future_basic_info'],
        public_pricing=dict(pro_monthly_cny=299,max_m1_monthly_cny=599,
            qualification='NOT_A_FUTURES_M1_L1_EXPORT_QUOTE; PRICING_COMPARISON_LISTS_FUTURES_DAILY',
            source='https://v6.bigquant.com/pricing'),
        vendor_quote_status='WRITTEN_SCOPE_AND_ENTITLEMENT_QUOTE_REQUIRED',
        next_material='2025_MAR_DEC_ACTUAL_LEG_M1_PLUS_SELECTED_PERIOD_L1_AND_HISTORICAL_SESSIONS_MAPPING_COSTS',
        calendar_lookahead_required_through='2026-01-12_METADATA_ONLY_NO_PROTECTED_PRICES',
        requests_must_be_batched='ONE_ACTUAL_CONTRACT_DAY_AT_A_TIME; CONFIRM_EXPORT_AND_LOCAL_RESEARCH_LICENSE'))
    instruments=','.join("'"+r['actual_bigquant']+"'" for r in rows)
    sql=("-- TEMPLATE ONLY: no authenticated export was run. Split by actual contract and day.\n"
         "-- date is M1 END; L1 date is event time; trading_day supplies night ownership.\n"
         "SELECT date,instrument,trading_day,open,high,low,close,volume,amount,open_interest\n"
         "FROM cn_future_bar1m_c\nWHERE instrument IN ("+instruments+")\n"
         "AND date >= TIMESTAMP '2025-03-03 00:00:00' AND date < TIMESTAMP '2026-01-01 00:00:00'\n"
         "ORDER BY instrument,date;\n\n"
         "-- First confirm entitlement and exact L1 column names against the table schema.\n"
         "-- Required: date,trading_day,instrument,bid/ask one price AND size,last,upper/lower limit.\n"
         "-- Request selected windows plus next-trading-day old-leg exit tails from request CSV.\n")
    (ROOT/'tools/short_swing_v9/bigquant_request_template.sql').write_text(sql,encoding='utf8')
    print('V9_ONE_ACCESS_ATTEMPT_CLOSED',len(rows),'actual legs; no prices or PnL',flush=True)


if __name__=='__main__':main()
