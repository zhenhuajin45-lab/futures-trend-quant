"""Read-only calendar, session and mapping source evidence. No strategy callbacks."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import check_lock, write


def main():
    output=Path(sys.argv[1])
    output.parent.mkdir(parents=True,exist_ok=True)
    result=dict(status="STARTED",observed_at=datetime.now(timezone.utc).isoformat(),calendars={},sessions={},mapping={})
    write(output,result)
    from gm.api import set_token, get_trading_dates, get_trading_session, fut_get_continuous_contracts
    set_token(os.environ["GM_TOKEN"])
    for exchange in ("CZCE","DCE","GFEX","SHFE"):
        try:
            result["calendars"][exchange]=get_trading_dates(exchange,"2026-11-02","2026-11-06")
        except Exception as exc:
            result["calendars"][exchange]={"error_type":type(exc).__name__}
        write(output,result)
    for symbol in check_lock()["universe"]:
        try:
            rows=fut_get_continuous_contracts(symbol,"2026-11-03","2026-11-04")
            result["mapping"][symbol]=rows
            actual=rows[-1]["symbol"] if rows else symbol
            result["sessions"][symbol]=get_trading_session(actual)
        except Exception as exc:
            result["sessions"][symbol]={"error_type":type(exc).__name__}
        result["last_observed_at"]=datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding="utf8")
    result["status"]="PROBE_COMPLETE"
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding="utf8")


if __name__=="__main__":
    main()
