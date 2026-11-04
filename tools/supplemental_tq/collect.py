"""Optional independent TQ market-data journal. No order/account operations."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import time

try:
    from .credentials import authentication
except ImportError:
    from credentials import authentication


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def actual_symbols(values):
    symbols=list(dict.fromkeys(values))
    if not symbols:
        raise ValueError("explicit actual-contract list required")
    for symbol in symbols:
        if not (re.fullmatch(r"CZCE\.[A-Z]+\d{3}",symbol) or
                re.fullmatch(r"(?:SHFE|DCE|INE|GFEX)\.[a-z]+\d{4}",symbol) or
                re.fullmatch(r"CFFEX\.[A-Z]+\d{4}",symbol)):
            raise ValueError("invalid actual-contract identity: "+symbol)
    return symbols


def prerequisites():
    try:
        authentication()
        configured=True
    except Exception:
        configured=False
    return dict(tqsdk_installed=importlib.util.find_spec("tqsdk") is not None,
                credentials_configured=configured,
                network_login_tested=False,market_coverage_tested=False,orders_enabled=False)


class Journal:
    def __init__(self,path):
        self.file=path.open("x",encoding="utf8")
        self.previous="0"*64
        self.sequence=0

    def append(self,kind,**payload):
        self.sequence+=1
        body=dict(sequence=self.sequence,source="TQ",kind=kind,received_at=utc_now(),
                  received_monotonic_ns=time.monotonic_ns(),previous_sha256=self.previous,**payload)
        encoded=json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
        self.previous=hashlib.sha256(encoded.encode("utf8")).hexdigest()
        self.file.write(json.dumps(dict(body,sha256=self.previous),ensure_ascii=False,sort_keys=True)+"\n")
        self.file.flush()

    def close(self):
        self.file.close()


def scalar(value):
    if hasattr(value,"item"):
        value=value.item()
    return None if isinstance(value,float) and not math.isfinite(value) else value


def run(symbols,output,seconds):
    # Optional SDK is imported only after the local prerequisite check.
    from tqsdk import TqApi,TqAuth,TqSim
    output.mkdir(parents=True,exist_ok=False)
    journal=Journal(output/"raw.jsonl")
    journal.append("START",symbols=symbols,decision_at="UNKNOWN",mapping_known_at="UNKNOWN",
                   clock_basis="LOCAL_FIRST_OBSERVATION_NOT_EXCHANGE_ARRIVAL",sdk_version=__import__("tqsdk").__version__)
    api=None
    try:
        user,password=authentication()
        api=TqApi(account=TqSim(),auth=TqAuth(user,password),web_gui=False,debug=False)
        quotes={s:api.get_quote(s) for s in symbols}
        klines={s:api.get_kline_serial(s,60,data_length=300) for s in symbols}
        prior_current={}
        observed={}
        start=last=time.monotonic()
        while time.monotonic()-start<seconds:
            changed=api.wait_update(deadline=time.time()+1)
            now=time.monotonic()
            gap=now-last
            if not changed:
                if gap>=10:
                    journal.append("NO_UPDATE",gap_seconds=gap)
                    last=now
                continue
            last=now
            for symbol,quote in quotes.items():
                if api.is_changing(quote):
                    fields=("datetime","last_price","bid_price1","bid_volume1","ask_price1","ask_volume1","volume","open_interest",
                            "instrument_id","exchange_id","ins_class","expired","price_tick","volume_multiple")
                    journal.append("QUOTE",symbol=symbol,payload={k:scalar(getattr(quote,k,None)) for k in fields})
            for symbol,frame in klines.items():
                if not api.is_serial_ready(frame) or not api.is_changing(frame):
                    continue
                last_time=scalar(frame.iloc[-1]["datetime"])
                if last_time is None or last_time<=0:
                    continue
                current=int(last_time)
                previous=prior_current.get(symbol)
                for row in frame.iloc[:-1].to_dict("records"):
                    raw_time=scalar(row["datetime"])
                    if raw_time is None:
                        continue
                    bob=int(raw_time)
                    if bob<=0 or bob>=current:
                        continue
                    payload={k:scalar(row.get(k)) for k in ("id","datetime","open","high","low","close","volume","open_oi","close_oi")}
                    if any(payload[k] is None for k in ("open","high","low","close")):
                        continue
                    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
                    key=(symbol,bob)
                    if observed.get(key)==digest:
                        continue
                    revision=key in observed
                    provenance="OBSERVED_BAR_ROLLOVER" if previous==bob and current>previous and gap<10 and not revision else "SNAPSHOT_OR_RECOVERY_OR_REVISION"
                    stamp=datetime.fromtimestamp(bob/1e9,timezone.utc)
                    journal.append("CLOSED_M1",symbol=symbol,bob=stamp.isoformat(),eob=(stamp+timedelta(minutes=1)).isoformat(),
                                   provenance=provenance,revision=revision,payload=payload,decision_at="UNKNOWN")
                    observed[key]=digest
                prior_current[symbol]=current
        journal.append("END",reason="REQUESTED_DURATION",unique_observed_minutes=len(observed))
    except BaseException as exc:
        # Avoid persisting authentication exception messages that may contain credentials.
        category="AUTH_REJECTED" if "用户权限认证失败" in str(exc) else ("NETWORK_TIMEOUT" if "Timeout" in type(exc).__name__ else "SDK_ERROR")
        journal.append("ERROR",error_type=type(exc).__name__,category=category)
        raise RuntimeError("TQ collection stopped; inspect local error type in journal") from None
    finally:
        if api is not None:
            api.close()
        journal.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--doctor",action="store_true")
    parser.add_argument("--symbols",nargs="+")
    parser.add_argument("--output",type=Path)
    parser.add_argument("--seconds",type=int,default=3600)
    args=parser.parse_args()
    status=prerequisites()
    if args.doctor:
        print(json.dumps(status,sort_keys=True))
        return
    if not all(status[k] for k in ("tqsdk_installed","credentials_configured")):
        raise SystemExit("PENDING_ACCOUNT_OR_SDK; run --doctor; no network connection attempted")
    if args.output is None or args.seconds<=0:
        raise SystemExit("fresh output directory and positive duration required")
    run(actual_symbols(args.symbols or []),args.output,args.seconds)


if __name__=="__main__":
    main()
