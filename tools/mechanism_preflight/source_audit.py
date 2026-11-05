"""One-shot source evidence audit; optional GM calendar/mapping, NEVER prices.

No history(), subscriptions, authentication retries or paid API is used. Existing
denials are evidence with their original observation time, not a new permission test.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import importlib.util
import json
import os
import pandas as pd
from .common import ROOT, read, write, sha, publish, output_target


def gm_metadata():
    result = dict(kind='CALENDAR_AND_MAPPING_ONLY', price_requests=0, retries=0,
                  query=['2024-01-01', '2025-08-31'], csymbol='DCE.M')
    if not os.environ.get('GM_TOKEN') or importlib.util.find_spec('gm') is None:
        return dict(result, status='NO_EXISTING_GM_AUTH_ENVIRONMENT'), None
    try:
        from gm.api import set_token, get_trading_dates, fut_get_continuous_contracts
        set_token(os.environ['GM_TOKEN'])
        dates = get_trading_dates('DCE', '2024-01-01', '2025-08-31')
        mapping = fut_get_continuous_contracts(csymbol='DCE.M', start_date='2024-01-01', end_date='2025-08-31')
        days = sorted(str(x)[:10] for x in dates)
        grouped = {}
        for row in mapping:
            symbol = row['symbol']; day = str(row['trade_date'])[:10]
            grouped.setdefault(symbol, []).append(day)
        coverage = [dict(symbol=symbol, first_mapping_date=min(ds), last_mapping_date=max(ds),
                         mapped_days=len(set(ds))) for symbol,ds in sorted(grouped.items())]
        return dict(result, status='METADATA_RECEIVED_NOT_M1_PERMISSION', calendar_days=len(days),
                    actual_contracts=coverage, historical_mapping_known_at='UNKNOWN'), dict(dates=days, mapping=mapping)
    except Exception as exc:
        # Do not echo remote exceptions, which can include credentials or account identifiers.
        return dict(result, status='METADATA_REQUEST_FAILED_NO_RETRY', error_type=type(exc).__name__), None


def run(inventory, out, request_gm_metadata=False, tq_status_evidence=None, reuse_gm_receipt=None):
    output_target(out)
    if request_gm_metadata and reuse_gm_receipt:
        raise ValueError('request and reuse are mutually exclusive')
    inv = Path(inventory); inv = inv/'inventory.csv' if inv.is_dir() else inv
    binding = read(inv.parent/'index_binding.json')
    if sha(inv) != binding['inventory_sha256']:
        raise ValueError('inventory digest mismatch')
    df = pd.read_csv(inv)
    # Existing footer-derived metadata, never opening market-data bodies.
    m = df.loc[df.contracts.fillna('').str.contains(r'DCE\.m\d', regex=True) & df.min_event_at.notna()
               & df.path.str.endswith('.parquet') & (df.finest_resolution == '60s')]
    summaries = m[['sha256','min_event_at','max_event_at','contracts']].drop_duplicates()
    gm_denial = read(ROOT/'reports/v7_source_probe.json')
    supply = read(ROOT/'reports/v71_source_supply_checks.json')
    edb = [r for r in supply['historical_clock_only_probes'] if r.get('csymbol') == 'DCE.M']
    reused_sha = None
    if reuse_gm_receipt:
        p = Path(reuse_gm_receipt)
        manifest = read(p.parent/'manifest.json')
        if sha(p) != manifest['files'][p.name]:
            raise ValueError('metadata receipt digest mismatch')
        meta, original = read(p)['gm_metadata'], None
        reused_sha = sha(p)
    else:
        meta, original = gm_metadata() if request_gm_metadata else (dict(status='NOT_REQUESTED', price_requests=0), None)
    tq = dict(status='NO_WORKING_SDK_AUTHORIZATION_PROVEN', history_requests=0, retries=0)
    if tq_status_evidence:
        p = Path(tq_status_evidence)
        text = p.read_text(encoding='utf-8-sig')
        if 'AUTH_REJECTED' not in text:
            raise ValueError('TQ authentication evidence does not establish rejected state')
        tq.update(evidence_sha256=sha(p), evidence_kind='EXISTING_LOCAL_AUTH_STATUS_NOT_NEW_LOGIN_TEST')
    result = dict(
        status='RESOURCE_UNRESOLVED_CLOSED', observed_at=datetime.now(timezone.utc).isoformat(),
        interpretation='NO_QUALIFIED_NEW_M1_SOURCE_ESTABLISHED_IN_EXISTING_AUTHORIZATIONS; NOT_A_GLOBAL_VENDOR_UNAVAILABILITY_CLAIM',
        source_check_complete=True, qualified_new_m1_sources=0, new_price_requests=0,
        metadata_requests_enabled=request_gm_metadata, reused_metadata_receipt_sha256=reused_sha,
        no_orders=True, no_subscriptions=True, no_procurement=True,
        original_inventory_sha256=sha(inv), prior_access_audit_scope='EXISTING_PROJECT_SOURCE_INDEX_1152_FILES_NOT_ALL_COMPUTER_OR_ALL_HUMAN_RESEARCH',
        indexed_m1_footer_ranges=summaries.to_dict('records'), gm_metadata=meta,
        options=[
            dict(source='GM', fields='symbol,bob,eob,open,high,low,close,volume,position',
                 existing_auth_environment=bool(os.environ.get('GM_TOKEN')), new_m1_readable_range='NOT_ESTABLISHED',
                 permission_evidence=gm_denial, entitlement_as_of=gm_denial.get('observed_at'),
                 older_year_action='NO_RETRY: LAST_OBSERVED_2002_EARLIEST_2026-05-08_COVERS_EARLIER_YEARS',
                 available_old_contracts=['DCE.m2609','DCE.m2701'],
                 restriction='EXISTING_2026_RESEARCH_RANGE_ALREADY_USED; FORMAL90_PROTECTED; DATE_GAPS_NOT_QUALIFIED',
                 calendar_mapping='METADATA_ONLY_NOT_PROOF_OF_FIRST_KNOWN_TIME_OR_MINUTE_PERMISSION'),
            dict(source='EDB_FREE', official_scope='ROLLING_RECENT_ONE_YEAR_M1; NO_TOKEN',
                 official_reference='https://doc.shinnytech.com/edb/latest/md_server.html',
                 fields='datetime_nano,open,high,low,close,volume,open_oi,close_oi',
                 timestamp='BOB_NANO; EOB=BOB+60S', prior_DCE_M_evidence=edb,
                 free_scope_as_of='2026-11-05; APPROXIMATE_BOUNDARY_NOT_ENDPOINT_PERMISSION_PROBE',
                 earlier_new_years='BEFORE_2025-09-01_OUTSIDE_FREE_MINUTE_SCOPE',
                 paid_authorization='NO_ACCEPTED_EXISTING_PRO_AUTHORIZATION_PROVEN',
                 restriction='SUPPORTED_LONG_WINDOWS_OVERLAP_PROTECTED_OR_ALREADY_USED_SAMPLES; SHORT_GAPS_NOT_AUTOMATIC_NEW_DATA',
                 action='NO_RETRY_OF_EXISTING_DENIED_RANGES; NO_PAID_TOKEN_LOGIN'),
            dict(source='TQSDK', **tq, paid_authorization='WEB_TRIAL_SCREENSHOT_DOES_NOT_PROVE_WORKING_SDK_OR_HISTORICAL_EXPORT_PERMISSION'),
            dict(source='BIGQUANT_JQDATA_TUSHARE', existing_authorization='USER_CONFIRMED_NONE',
                 readable_range='UNKNOWN_NOT_QUERIED', action='NO_NEW_ACCOUNT_PURCHASE_OR_VENDOR_CONTACT')],
        protected_samples_unchanged=True, independent_test_consumed=False,
        next_action='CLOSE_THIS_RESOURCE_ATTEMPT; NO_RECURRING_WAIT_OR_RETRY; NEW_EXPLICIT_SOURCE_EVIDENCE_REQUIRED_TO_REOPEN')
    def build(stage):
        write(stage/'m1_source_options.local.json', result)
        if original is not None:
            write(stage/'gm_calendar_mapping.local.json', original)
    publish(out, build)
    return dict(status=result['status'], gm_metadata_status=meta['status'], new_price_requests=0,
                qualified_new_m1_sources=0, metadata_contracts=len(meta.get('actual_contracts',[])))


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--inventory', type=Path, required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--gm-metadata',action='store_true');p.add_argument('--tq-status-evidence',type=Path)
    p.add_argument('--reuse-gm-receipt',type=Path)
    args=p.parse_args()
    print(json.dumps(run(args.inventory,args.out,args.gm_metadata,args.tq_status_evidence,args.reuse_gm_receipt)),flush=True)


if __name__=='__main__':main()
