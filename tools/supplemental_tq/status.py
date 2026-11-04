"""Local, redacted TQ journal verification. No network, scoring or sealing."""
import argparse
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path


def inspect(path):
    previous="0"*64
    sequence=0
    kinds=Counter()
    quotes=Counter()
    fresh=Counter()
    closed=defaultdict(set)
    rolled=defaultdict(set)
    revisions=Counter()
    exchange_times={}
    received_times={}
    errors=[]
    ended=False
    requested=[]
    start=None
    with path.open(encoding="utf8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                break
            value=json.loads(line)
            claimed=value.pop("sha256")
            encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
            assert value["previous_sha256"]==previous and value["sequence"]==sequence+1,"chain order mismatch"
            assert hashlib.sha256(encoded.encode()).hexdigest()==claimed,"chain content mismatch"
            previous=claimed
            sequence+=1
            kind=value["kind"]
            kinds[kind]+=1
            if kind=="START":
                requested=value["symbols"]
                start=datetime.fromisoformat(value["received_at"])
            if kind=="QUOTE":
                symbol=value["symbol"]
                payload=value["payload"]
                if payload.get("last_price") is None or not payload.get("datetime"):
                    continue
                quotes[symbol]+=1
                exchange_times[symbol]=payload["datetime"]
                received_times[symbol]=value["received_at"]
                t=datetime.fromisoformat(payload["datetime"])
                if t.tzinfo is None:
                    t=t.replace(tzinfo=timezone(timedelta(hours=8)))
                fresh[symbol]+=t>=start
            if kind=="CLOSED_M1":
                symbol=value["symbol"]
                closed[symbol].add(value["eob"])
                if value["provenance"]=="OBSERVED_BAR_ROLLOVER":
                    rolled[symbol].add(value["eob"])
                revisions[symbol]+=value["revision"]
            if kind=="ERROR":
                errors.append({k:value[k] for k in ("error_type","category")})
            ended|=kind=="END"
    return dict(chain_valid=True,verified_rows=sequence,last_verified_sha256=previous,
                start_received_at=None if start is None else start.isoformat(),ended=ended,kinds=dict(kinds),errors=errors,
                requested_contracts=len(requested),contracts_with_quotes=sum(quotes[s]>0 for s in requested),
                contracts_with_post_start_exchange_updates=sum(fresh[s]>0 for s in requested),
                contracts_with_observed_closed_m1=sum(bool(rolled[s]) for s in requested),
                by_contract={s:dict(quote_snapshots=quotes[s],post_start_exchange_updates=fresh[s],
                    unique_closed_m1_including_snapshots=len(closed[s]),observed_rollover_closed_m1=len(rolled[s]),
                    closed_revisions=revisions[s],latest_exchange_time=exchange_times.get(s),latest_received_at=received_times.get(s)) for s in requested},
                full_session_coverage_verified=False,strict_forward_qualification="NOT_EVALUATED",orders_enabled=False,
                decision_at="UNKNOWN",historical_mapping_publication="UNKNOWN")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--journal",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    result=inspect(args.journal)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf8")
    print(json.dumps(result,ensure_ascii=False))
