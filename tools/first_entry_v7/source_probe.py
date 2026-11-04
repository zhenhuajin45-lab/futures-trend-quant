"""One bounded historical request; never subscribe or retry other providers."""
import json
import os
from pathlib import Path
import sys
import re
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backtest.forward_v3 import write


def main():
    request = dict(symbol='DCE.m2605', frequency='60s', start_time='2026-01-05 09:00:00',
                   end_time='2026-01-05 09:05:00', fields='symbol,bob,eob,open,high,low,close,volume,position',
                   adjust=0, df=True)
    result = dict(request=request, observed_at=datetime.now(timezone.utc).isoformat(), no_subscriptions=True,
                  no_orders=True, raw_prices_published=False)
    try:
        from gm.api import set_token, history
        set_token(os.environ['GM_TOKEN'])
        rows = history(**request)
        result.update(status='ROWS_RECEIVED' if len(rows) else 'NO_ROWS', rows=len(rows))
        if len(rows):
            local = ROOT/'data/cache/first_entry_v7_source'
            local.mkdir(parents=True, exist_ok=True)
            rows.to_parquet(local/'probe_DCE_m2605.parquet', index=False)
    except Exception as exc:
        # Credential values can occur in provider exceptions; redact before any output.
        message = str(exc)
        for key in ('GM_TOKEN', 'TQ_PASSWORD', 'TQ_USER'):
            if os.environ.get(key):
                message = message.replace(os.environ[key], '[REDACTED]')
        code = re.search(r'"status"\s*:\s*(\d+)', message)
        dates = re.findall(r'202\d-\d\d-\d\d', message)
        result.update(status='REQUEST_FAILED', error_type=type(exc).__name__,
            provider_status=int(code.group(1)) if code else None,
            no_data_permission='ERR_NO_DATA_PERMISSION' in message,
            earliest_query_date=dates[-1] if dates else None,
            error='HISTORICAL_PERMISSION_DENIED' if 'ERR_NO_DATA_PERMISSION' in message else 'SEE_LOCAL_EXCEPTION')
    write(ROOT/'reports/v7_source_probe.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
