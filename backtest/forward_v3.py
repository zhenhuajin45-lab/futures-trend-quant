"""Append-only, manually fed forward study. Data and results are local to the study."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import os
import tempfile

import pandas as pd

from futures_trend_quant.config import load_config
from futures_trend_quant.data.gm_bars import bars_from_records
from .aggregation import aggregate_bars
from .exit_rule_experiment import _run_product
from .forward_scores import GROUPS, decide, summarize_product, summarize_opportunities

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "config" / "forward_v3_lock.json"
FILES = ("sessions.json", "quotes.parquet", "mapping.parquet")
SEALED = ("groups.json", "events.json", "opportunities.json", "rolls.json", "decision.json", "decision.md")
CHINA = timezone(timedelta(hours=8))


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)+"\n", encoding="utf-8")


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def code_hash():
    h = sha256()
    paths = [ROOT / "config/strategy.json"]
    for folder in ("backtest", "futures_trend_quant"):
        paths.extend((ROOT / folder).rglob("*.py"))
    for path in sorted(paths):
        h.update(path.relative_to(ROOT).as_posix().encode()+b"\0")
        h.update(path.read_bytes().replace(b"\r\n", b"\n")+b"\0")
    return h.hexdigest()


@contextmanager
def publish_dir(destination):
    """Same-volume atomic directory publication, no partial manifests on retry."""
    destination.parent.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        stage = Path(temp) / "stage"
        stage.mkdir()
        yield stage
        if destination.exists():
            raise ValueError(f"already sealed: {destination.name}")
        stage.rename(destination)


def check_lock():
    lock = read(LOCK)
    if code_hash() != lock["code_sha256"]:
        raise ValueError("frozen code/config hash mismatch")
    return lock


def prepare(study):
    lock = check_lock()
    with publish_dir(study) as target:
        write(target/"protocol.json", lock)
        (target/"days").mkdir()
    return {"status":"READY_FOR_DATA", "complete_days":0, "required_days":90}


def stamp(value):
    t = pd.Timestamp(value)
    if t.tzinfo is None:
        raise ValueError("timestamps require an explicit offset")
    return t.tz_convert(CHINA)


def expected_times(segments):
    """Intervals are exchange minutes (start, end], including the night of this trading day."""
    minutes = []
    for start, end in segments:
        a, b = stamp(start), stamp(end)
        if a.floor("min") != a or b.floor("min") != b or not timedelta(0) < b-a <= timedelta(hours=12):
            raise ValueError("invalid session")
        minutes.extend(pd.date_range(a+timedelta(minutes=1), b, freq="min"))
    if not minutes or sorted(set(minutes)) != minutes:
        raise ValueError("empty, unordered or overlapping sessions")
    return minutes


def calendar_record(session, lock):
    minutes = {s:expected_times(v) for s,v in session["products"].items()}
    if set(minutes) != set(lock["universe"]):
        raise ValueError("calendar universe drift")
    first = min(v[0] for v in minutes.values()) - timedelta(minutes=1)
    last = max(v[-1] for v in minutes.values())
    return dict(date=session["date"], previous=session["previous"], first_open=first.isoformat(),
                last_close=last.isoformat(), scoring=first > stamp(lock["freeze_time"]), source=session["source"])


def checked_days(study):
    lock = check_lock()
    if read(study/"protocol.json") != lock:
        raise ValueError("study protocol mismatch")
    days = []
    for path in sorted((study/"days").glob("????-??-??")):
        record = read(path/"receipt.json")
        canonical = calendar_record(read(path/"sessions.json"), lock)
        if (record["date"] != path.name or record["hashes"] != {f:digest(path/f) for f in FILES}
                or any(record[key] != value for key,value in canonical.items())
                or (days and days[-1]["date"] != record["previous"])):
            raise ValueError(f"immutable day chain mismatch: {path.name}")
        days.append(record)
    return lock, days


def ingest(study, bundle):
    guard = study/".ingest_in_progress"
    # An interrupted writer leaves an explicit recoverable lock; never fork the day chain.
    with guard.open("x") as stream:
        stream.write(str(os.getpid()))
    try:
        return _ingest(study, bundle)
    finally:
        guard.unlink()


def _ingest(study, bundle):
    lock, days = checked_days(study)
    session = read(bundle/"sessions.json")
    trading = date.fromisoformat(session["date"]).isoformat()
    previous = date.fromisoformat(session["previous"]).isoformat()
    if previous >= trading or not session["source"] or set(session["products"]) != set(lock["universe"]):
        raise ValueError("invalid calendar, source or universe")
    minute_sets = {s:expected_times(v) for s,v in session["products"].items()}
    first_open = min(v[0] for v in minute_sets.values())-timedelta(minutes=1)
    last_close = max(v[-1] for v in minute_sets.values())
    if (last_close.date().isoformat() != trading or first_open.date().isoformat() < previous
            or last_close > datetime.now(CHINA)):
        raise ValueError("session date bounds or final close invalid")
    snapshot = {f:digest(bundle/f) for f in FILES}
    destination = study/"days"/trading
    if destination.exists():
        if snapshot != read(destination/"receipt.json")["hashes"]:
            raise ValueError("conflicting daily revision")
        return {"status":"ALREADY_INGESTED", "date":trading}
    if days and (days[-1]["date"] != previous or stamp(days[-1]["last_close"]) >= first_open):
        raise ValueError("non-consecutive exchange day or overlapping sessions")
    is_score = first_open > stamp(lock["freeze_time"])
    if is_score and (not days or not any(not r["scoring"] for r in days)):
        raise ValueError("ingest consecutive pre-freeze warmup and preceding trading day first")
    if sum(r["scoring"] for r in days) >= 90:
        raise ValueError("fixed horizon closed")
    quotes, mapping = (pd.read_parquet(bundle/f) for f in FILES[1:])
    for frame, required in ((quotes, {"csymbol","trading_date","eob","bob","symbol","available_at","open","high","low","close"}),
                            (mapping, {"csymbol","trading_date","symbol","known_at"})):
        missing = required - set(frame)
        if missing:
            raise ValueError(f"missing source columns {sorted(missing)}")
    for frame in (quotes, mapping):
        if set(frame["trading_date"].astype(str)) != {trading} or set(frame["csymbol"]) - set(lock["universe"]):
            raise ValueError("unexpected date/product in daily package")
    counts = {}
    for s, minutes in minute_sets.items():
        q = quotes[quotes.csymbol == s]
        observed = set(map(stamp, q.eob))
        schedule = set(minutes)
        if observed - schedule:
            raise ValueError(f"{s} raw quote outside declared exchange session")
        subset = mapping[mapping.csymbol == s]
        counts[s] = {"expected":len(minutes), "missing_any_leg":len(schedule-observed),
                     "quote_rows":len(q), "mapping_rows":len(subset),
                     "unknown_mapping_publication":int(subset.known_at.isna().sum()),
                     "unknown_quote_availability":int(q.available_at.isna().sum())}
    record = dict(date=trading, previous=previous, scoring=is_score, first_open=first_open.isoformat(),
                  last_close=last_close.isoformat(), hashes=snapshot, counts=counts, source=session["source"])
    with publish_dir(destination) as stage:
        for f in FILES:
            shutil.copyfile(bundle/f, stage/f)
            if digest(stage/f) != snapshot[f]:
                raise ValueError("bundle changed during import")
        write(stage/"receipt.json", record)
    return {"status":"INGESTED", **record}


def evaluate(study, calendar_manifest=None):
    """Only runs after the last complete session. Verify all products before scoring any group."""
    lock, records = checked_days(study)
    days = [r["date"] for r in records if r["scoring"]]
    if len(days) < 90:
        return {"status":"READY_FOR_DATA", "score_days":len(days), "required":90}
    if calendar_manifest is None:
        raise ValueError("missing data owner's independent calendar manifest")
    calendar = read(calendar_manifest)
    if (not calendar.get("authority") or not calendar.get("version")
            or calendar.get("session_sha256_by_day") != {r["date"]:r["hashes"]["sessions.json"] for r in records}):
        raise ValueError("calendar/source attestation does not match sealed sessions")
    if len(days) != 90 or days[0] <= "2026-11-02" or stamp(records[-1]["last_close"]) > datetime.now(CHINA):
        raise ValueError("fixed horizon or sample separation failed")
    from .asof_roll import build_asof_minutes
    dest = study/"unblinded"
    data_id = sha256(json.dumps([r["hashes"] for r in records], sort_keys=True).encode()).hexdigest()
    if dest.exists():
        decision = read(dest/"decision.json")
        if (decision["data_sha256"] != data_id or decision["protocol"] != lock
                or decision["calendar_sha256"] != digest(calendar_manifest)
                or read(dest/"checksums.json") != {name:digest(dest/name) for name in SEALED}):
            raise ValueError("unexpected completed study identity")
        return decision
    inputs = [study/"days"/r["date"] for r in records]
    quotes = pd.concat([pd.read_parquet(p/FILES[1]) for p in inputs], ignore_index=True)
    mapping = pd.concat([pd.read_parquet(p/FILES[2]) for p in inputs], ignore_index=True)
    sessions = [read(p/FILES[0]) for p in inputs]
    config = load_config(ROOT/"config/strategy.json")
    with publish_dir(dest) as stage:
        with tempfile.TemporaryDirectory(dir=stage) as tmp:
            tmp = Path(tmp)
            products, pairs, opportunities, raw_paths, roll_events = {}, [], [], {}, {}
            for s in lock["universe"]:
                q = quotes[quotes.csymbol == s].copy()
                m = mapping[mapping.csymbol == s].copy()
                frame, rolls = build_asof_minutes(s, m, q)
                roll_events[s] = [{key: value.isoformat() if hasattr(value, "isoformat") else value
                                   for key,value in roll.items()} for roll in rolls]
                expected = [t for day in sessions for t in expected_times(day["products"][s])]
                actual = list(map(stamp, frame.eob))
                if (actual != expected or not frame.eligible.eq(True).all()):
                    bad = frame.loc[~frame.eligible.eq(True), ["eob","reason"]].head(3).to_dict("records")
                    missing = sorted(set(expected)-set(actual))[:3]
                    raise ValueError(f"DATA_BLOCKED {s}: gaps {missing}, ineligible {bad}")
                frame["trading_date"] = frame.trading_date.astype(str)
                before = frame[frame.trading_date < days[0]]
                warm = aggregate_bars(bars_from_records(before.to_dict("records")), minutes=15, frequency="900s")
                if len(warm) < 1200:
                    raise ValueError(f"DATA_BLOCKED {s}: warmup={len(warm)}")
                # Validate one actual contract per score M15 and complete its raw price bars.
                score_frame = frame[frame.trading_date >= days[0]].copy()
                score_frame["boundary"] = [stamp(v).ceil("15min") for v in score_frame.eob]
                grouped = score_frame.groupby("boundary").actual_contract
                if grouped.nunique().ne(1).any():
                    raise ValueError(f"DATA_BLOCKED {s}: intrabar contract transition")
                contracts = grouped.first().to_dict()
                clocks = {stamp(t): stamp(c) for t,c in zip(frame.eob, frame.decision_at)}
                m15_clock = {t: clocks[t] for t in contracts}
                expected_counts = score_frame.boundary.value_counts().to_dict()
                raw15 = {}
                q["frequency"] = "60s"
                q = q[q.available_at.notna()].copy()
                q = q.loc[[stamp(a) <= clocks[stamp(t)] for a,t in zip(q.available_at, q.eob)]]
                for contract, rows in q.groupby("symbol"):
                    rows = rows.sort_values("eob")
                    raw_bars = bars_from_records(rows.to_dict("records"))
                    counts = pd.Series([stamp(t).ceil("15min") for t in rows.eob]).value_counts().to_dict()
                    for bar in aggregate_bars(raw_bars, minutes=15, frequency="900s"):
                        if counts.get(bar.eob) == expected_counts.get(bar.eob):
                            raw15[contract,bar.eob] = bar
                raw_paths[s] = contracts, raw15, m15_clock
                frame.to_parquet(tmp/(s+".parquet"), index=False)
            for s in lock["universe"]:
                replay = _run_product(str(tmp), s+".parquet", s, config.raw["rules"], config.subscription_count,
                                      GROUPS, date.fromisoformat(days[0]), date.fromisoformat(days[-1]))
                expected_clock = list(raw_paths[s][0])
                if ([bar.eob for bar in replay["bars"]] != expected_clock or
                    any([stamp(row["eob"]) for row in replay["groups"][g]] != expected_clock for g in GROUPS)):
                    raise ValueError(f"DATA_BLOCKED {s}: scored M15 clock differs from exchange calendar")
                summary, event_rows, op_rows = summarize_product(replay, *raw_paths[s])
                products[s] = summary
                pairs.extend(event_rows)
                opportunities.extend(op_rows)
        result = decide(products, pairs, days)
        result.update(protocol=lock, data_sha256=data_id, unblinded_at=datetime.now(CHINA).isoformat(),
                      calendar_sha256=digest(calendar_manifest), first_day=days[0], last_day=days[-1], per_product=products)
        result["opportunity_space8"] = summarize_opportunities(opportunities)
        write(stage/"groups.json", result["groups"])
        write(stage/"events.json", pairs)
        write(stage/"opportunities.json", opportunities)
        write(stage/"rolls.json", roll_events)
        result["command"] = "python -m backtest.forward_v3 score --study STUDY --calendar-manifest CALENDAR"
        result["exit_code"] = 0
        write(stage/"decision.json", result)
        text = ["# 独立 90 日复验决定", "",
                f"{days[0]} 至 {days[-1]}，{len(products)} 个品种，{result['complete']} 个完整事件，"
                f"{result['censored']} 个尾部截尾事件。", "",
                f"A 对 S0 救回 {result['rescued']} 个、伤害 {result['harmed']} 个及时转向。"
                f"局部机制判定为 {result['local_status']}。", "",
                "| 组别 | 未来首触方向一致率 | 趋势段覆盖 | 震荡切换/品种日 | 延迟中位数 | 四门槛通过 |",
                "|---|---:|---:|---:|---:|---|"]
        for group, outcome in result["groups"].items():
            values = [outcome["pool"][k] for k in
                      ("direction_agreement", "trend_segment_coverage", "range_switches_per_product_day", "delay")]
            fields = ["无分母" if v is None else f"{v:.5f}" for v in values]
            text.append("| " + " | ".join([group, *fields, str(outcome["hard_gates_pass"])]) + " |")
        text.extend(["", f"下一步 {result['next_step']}。", "",
                     f"数据 SHA256 `{data_id}`。评分程序与规则版本见同目录 decision.json。"])
        (stage/"decision.md").write_text("\n".join(text)+"\n", encoding="utf-8")
        write(stage/"checksums.json", {f:digest(stage/f) for f in SEALED})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare","ingest","score"))
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--calendar-manifest", type=Path)
    args = parser.parse_args()
    if args.phase == "ingest" and not args.bundle:
        parser.error("ingest requires --bundle")
    try:
        result = (prepare(args.study) if args.phase == "prepare" else ingest(args.study, args.bundle)
                  if args.phase == "ingest" else evaluate(args.study, args.calendar_manifest))
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print(f"DATA_BLOCKED: {exc}")
        return 2
    print(json.dumps({k:v for k,v in result.items() if k not in ("per_product","counts")}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
