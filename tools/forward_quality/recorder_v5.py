"""GM raw market evidence only. No strategy, position or execution callbacks."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
CN = timezone(timedelta(hours=8))
JOURNAL = None
RAW_BAR_FIELDS = ("symbol", "bob", "eob", "open", "close", "high", "low", "volume",
                  "amount", "pre_close", "position", "frequency", "receive_local_time")


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.stream = (self.directory/"raw.jsonl").open("xb", buffering=0)
        self.seq = 0
        self.previous = "0"*64
        self.counts = {}
        self.first = None
        self.last = None
        self.health_write_conflicts = 0

    def append(self, kind, payload, received_ns=None, annotations=None):
        received_ns = received_ns if received_ns is not None else time.time_ns()
        self.seq += 1
        event = dict(seq=self.seq, previous_sha256=self.previous, kind=kind,
                     received_unix_ns=received_ns, monotonic_ns=time.monotonic_ns(),
                     received_at=datetime.fromtimestamp(received_ns/1e9, CN).isoformat(),
                     payload=payload)
        if annotations is not None:
            event["annotations"] = annotations
        body = (json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)+"\n").encode("utf8")
        self.stream.write(body)
        os.fsync(self.stream.fileno())
        self.previous = hashlib.sha256(body).hexdigest()
        if kind == "BAR":
            symbol = payload["symbol"]
            self.counts[symbol] = self.counts.get(symbol, 0)+1
            self.first = self.first or event["received_at"]
            self.last = event["received_at"]
        return event

    def health(self, subscriptions):
        obj = dict(pid=os.getpid(), as_of=datetime.now(CN).isoformat(), seq=self.seq,
                   last_line_sha256=self.previous, callbacks_by_contract=self.counts,
                   first_callback_at=self.first, last_callback_at=self.last,
                   subscriptions=sorted(subscriptions), source="GM_LIVE_CALLBACK",
                   evidence_clock="LOCAL_FIRST_CALLBACK_OBSERVATION",
                   decision_clock="NOT_RECORDED; no decision_at generated",
                   health_write_conflicts=self.health_write_conflicts,
                   orders="NO_EXECUTION_CODE")
        temp = self.directory/"health.tmp"
        temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf8")
        try:
            temp.replace(self.directory/"health.json")
        except PermissionError:
            # Windows readers can briefly deny replacement. Raw fsync logging
            # remains active and the next timer retries the health snapshot.
            self.health_write_conflicts += 1


def journal():
    global JOURNAL
    if JOURNAL is None:
        JOURNAL = Journal(os.environ["FTQ_RECORD_RUN"])
    return JOURNAL


def refresh(context):
    from gm.api import fut_get_continuous_contracts, subscribe
    today = datetime.now(CN).date()
    mains, old_contracts = [], set()
    for symbol in context.universe:
        started = time.time_ns()
        try:
            rows = fut_get_continuous_contracts(symbol, str(today-timedelta(days=40)), str(today+timedelta(days=1)))
            observed = time.time_ns()
            journal().append("MAPPING_OBSERVATION", dict(csymbol=symbol, request_started_unix_ns=started,
                             response_observed_unix_ns=observed, records=rows,
                             clock_semantics="Observed by this recorder, not supplier publication"), observed)
            if rows:
                mains.append(rows[-1]["symbol"])
                old_contracts.update(r["symbol"] for r in rows)
        except Exception as exc:
            journal().append("MAPPING_OR_SUBSCRIBE_ERROR", dict(csymbol=symbol, error_type=type(exc).__name__,
                                                               message=str(exc)[:400]))
    # Main contracts have priority over archived legs. If the entitlement is
    # insufficient, capture a disclosed subset and keep all failures as evidence.
    mains.sort(key=lambda s: (not s.startswith(("SHFE.au","SHFE.ag")), s))
    for symbol in mains:
        if symbol in context.subscriptions:
            continue
        try:
            subscribe(symbols=symbol, frequency="60s", count=0, wait_group=False)
            context.subscriptions.add(symbol)
            journal().append("SUBSCRIBE", dict(symbols=[symbol], count=0, frequency="60s"))
        except Exception as exc:
            journal().append("SUBSCRIBE_ERROR", dict(symbol=symbol, error_type=type(exc).__name__,message=str(exc)[:400]))
    if set(mains).issubset(context.subscriptions):
        for symbol in sorted(old_contracts-set(mains)-context.subscriptions):
            try:
                subscribe(symbols=symbol, frequency="60s", count=0, wait_group=False)
                context.subscriptions.add(symbol)
                journal().append("SUBSCRIBE_OLD_LEG", dict(symbol=symbol))
            except Exception as exc:
                journal().append("SUBSCRIBE_ERROR", dict(symbol=symbol, error_type=type(exc).__name__,message=str(exc)[:400]))
    journal().append("COVERAGE", dict(main_contracts=mains, subscribed=sorted(context.subscriptions),
                     full_main_coverage=set(mains).issubset(context.subscriptions) and len(mains)==12,
                     missing_mains=sorted(set(mains)-context.subscriptions)))


def init(context):
    from gm.api import timer
    from backtest.forward_v3 import check_lock, read, digest, expected_times
    import gm.__version__ as version
    lock = check_lock()
    context.universe = lock["universe"]
    context.subscriptions = set()
    context.last_mapping = 0.
    context.last_health = 0.
    context.calendar_days = {}
    calendar_file = ROOT/"data/cache/forward_v4/calendar/calendar.json"
    calendar_hash = None
    if calendar_file.is_file():
        calendar = read(calendar_file)
        calendar_hash = digest(calendar_file)
        for day in calendar["sessions"]:
            for symbol, intervals in day["products"].items():
                context.calendar_days.setdefault(symbol, {}).update({t:day["date"] for t in expected_times(intervals)})
    journal().append("INIT", dict(universe=context.universe, sdk=getattr(version,"__version__",str(version)),
                                recorder_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                calendar_sha256=calendar_hash,
                                frozen_code_sha256=lock["code_sha256"], local_time_source="Windows wall clock + monotonic",
                                unseen_future_contracts="Only contracts returned by recorded 40-day mapping snapshots",
                                stop_at=os.environ.get("FTQ_RECORD_STOP","2026-11-05T15:05:00+08:00")))
    refresh(context)
    context.last_mapping = time.monotonic()
    result = timer(on_timer, period=10, start_delay=0)
    journal().append("TIMER", result)
    if result["status"] != 0:
        raise RuntimeError("health timer unavailable")
    journal().health(context.subscriptions)


def on_timer(context):
    from gm.api import stop
    if datetime.now(CN) >= datetime.fromisoformat(os.environ.get("FTQ_RECORD_STOP","2026-11-05T15:05:00+08:00")):
        journal().append("PLANNED_STOP", {})
        journal().health(context.subscriptions)
        stop()
        return
    if time.monotonic()-context.last_mapping >= 60:
        refresh(context)
        context.last_mapping = time.monotonic()
    journal().health(context.subscriptions)


def on_bar(context, bars):
    received = time.time_ns()
    for bar in bars:
        raw = {name: bar[name] for name in RAW_BAR_FIELDS}
        csymbol = "".join(ch for ch in raw["symbol"] if not ch.isdigit()).upper()
        trading_day = getattr(context,"calendar_days",{}).get(csymbol,{}).get(raw["eob"],"UNKNOWN")
        journal().append("BAR", raw, received, dict(trading_date=trading_day,
                         trading_date_basis="INDEPENDENT_CAPTURED_SESSION_METADATA_OR_UNKNOWN"))
    journal().health(context.subscriptions)


def on_market_data_connected(context):
    journal().append("MARKET_CONNECTED", {})


def on_market_data_disconnected(context):
    journal().append("MARKET_DISCONNECTED", {})


def on_error(context, code, info):
    journal().append("SDK_ERROR", dict(code=code, message=str(info)[:500]))


def on_shutdown(context):
    journal().append("SHUTDOWN", {})


if __name__ == "__main__":
    from gm.api import MODE_LIVE, run
    run(strategy_id=os.environ.get("FTQ_RECORD_STRATEGY_ID",""), filename="recorder_v5.py",
        mode=MODE_LIVE, token=os.environ["GM_TOKEN"])
