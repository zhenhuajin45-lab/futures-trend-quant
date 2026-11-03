"""Quality-only prefix admission around the immutable FTQ_FORWARD_90_V3."""
import argparse
import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pandas as pd
from backtest import forward_v3 as frozen
from backtest.asof_roll import build_asof_minutes
from backtest.aggregation import aggregate_bars
from futures_trend_quant.data.gm_bars import bars_from_records

BASE = "42825e538904a1b70f399586582451b90c893055"


def fail(reason, csymbol=None, eob=None):
    return dict(reason=reason, csymbol=csymbol, eob=None if eob is None else str(eob))


def provenance_status(bundle_root, manifest):
    path = bundle_root / "provenance.json"
    if not path.exists():
        return False, {"source_status": "UNKNOWN", "reason": "SOURCE_EVIDENCE_NOT_DELIVERED"}
    source = frozen.read(path)
    required = ["calendar", "mapping", "quote"]
    if manifest.get("decision_clock_mode") == "RECORDED":
        required.append("decision")
    for kind in required:
        item = source.get("evidence", {}).get(kind, {})
        evidence = (bundle_root / item.get("evidence_ref", "")).resolve()
        if (not evidence.is_relative_to(bundle_root.resolve()) or not evidence.is_file()
                or not item.get("owner") or item.get("source_status") != "VERIFIED"
                or frozen.digest(evidence) != item.get("sha256")):
            return False, {"source_status": item.get("source_status", "UNKNOWN"),
                           "reason": "SOURCE_EVIDENCE_UNVERIFIED", "kind": kind}
    # A hash establishes identity. The reviewer attests what these logs actually mean.
    if not source.get("independent_reviewer") or not source.get("owner"):
        return False, {"source_status": "UNKNOWN", "reason": "INDEPENDENT_SOURCE_REVIEW_PENDING"}
    return True, {"source_status": "VERIFIED", "owner": source["owner"],
                  "independent_reviewer": source["independent_reviewer"],
                  "provenance_sha256": frozen.digest(path)}


def inspect_product(symbol, quotes, mapping, sessions, first_day):
    expected = [t for session in sessions for t in frozen.expected_times(session["products"][symbol])]
    day_by_time = {t: session["date"] for session in sessions
                   for t in frozen.expected_times(session["products"][symbol])}
    received = set(map(frozen.stamp, quotes.eob)) if "eob" in quotes else set()
    result = dict(csymbol=symbol, expected_m1=len(expected), received_any_leg_m1=len(received & set(expected)),
                  strict_valid_m1=0, warmup_valid_m15=0, valid_m15=0, first_failure=None, days={})
    failures = []
    if received - set(expected):
        failures.append(fail("RAW_QUOTE_OUTSIDE_CALENDAR", symbol, min(received-set(expected))))
    for frame in (quotes, mapping):
        if not frame.empty and (frame["trading_date"].astype(str).isin([s["date"] for s in sessions]) == False).any():
            failures.append(fail("WRONG_TRADING_DATE", symbol))
    try:
        frame, rolls = build_asof_minutes(symbol, mapping, quotes)
    except (ValueError, KeyError, TypeError) as exc:
        result["first_failure"] = fail(str(exc), symbol)
        return result
    frame["eob"] = [frozen.stamp(t) for t in frame.eob]
    indexed = {r["eob"]: r for r in frame.to_dict("records")}
    if list(frame.eob) != sorted(set(frame.eob)):
        failures.append(fail("ASOF_CLOCK_ORDER", symbol))
    eligible = {}
    for t in expected:
        row = indexed.get(t)
        good = row is not None and row["eligible"] is True
        eligible[t] = good
        day = day_by_time[t]
        counts = result["days"].setdefault(day, dict(expected=0, received_any_leg=0, strict_valid=0, failures=0))
        counts["expected"] += 1
        counts["received_any_leg"] += int(t in received)
        counts["strict_valid"] += int(good)
        counts["failures"] += int(not good)
        if not good:
            reason = row["reason"] if row else ("NO_TIMELY_OUTPUT" if t in received else "MISSING_EXPECTED_MINUTE")
            failures.append(fail(reason, symbol, t))
    result["strict_valid_m1"] = sum(eligible.values())
    # Aggregate the whole clock. Count a bucket only when every scheduled input
    # is eligible, one actual contract supplies it, and the close clock exists.
    buckets = {}
    for t in expected:
        buckets.setdefault(t.ceil("15min"), []).append(t)
    good_rows = frame.loc[frame.eligible.eq(True)].to_dict("records")
    bars = aggregate_bars(bars_from_records(good_rows), minutes=15, frequency="900s") if good_rows else []
    emitted = {b.eob for b in bars}
    for boundary, times in buckets.items():
        if not all(eligible[t] for t in times):
            continue
        identities = {indexed[t]["actual_contract"] for t in times}
        if len(identities) != 1:
            failures.append(fail("M15_MIXED_ACTUAL_CONTRACTS", symbol, boundary))
        elif boundary not in indexed or boundary not in emitted:
            failures.append(fail("M15_NO_CLOSED_DECISION_CLOCK", symbol, boundary))
        else:
            result["valid_m15"] += 1
            if first_day is not None and day_by_time[times[-1]] < first_day:
                result["warmup_valid_m15"] += 1
    if first_day and result["warmup_valid_m15"] < 1200:
        failures.append(fail("WARMUP_M15_BELOW_1200", symbol))
    result["first_failure"] = min(failures, key=lambda f: f["eob"] or "9999") if failures else None
    result["failure_counts"] = dict(pd.Series([f["reason"] for f in failures]).value_counts().items()) if failures else {}
    result["rolls"] = [{k: str(v) for k,v in row.items() if k not in
                       ("old_raw_close", "new_raw_close", "delta", "cumulative_offset")} for row in rolls]
    cutoff = quotes.decision_at if "decision_at" in quotes else quotes.eob
    result["late_nonselected_quote_warnings"] = sum(
        pd.notna(a) and frozen.stamp(a) > frozen.stamp(c)
        and indexed.get(frozen.stamp(t), {}).get("actual_contract") != s
        for a,c,t,s in zip(quotes.available_at, cutoff, quotes.eob, quotes.symbol))
    return result


def preflight(bundle_root, calendar_manifest, study=None, now=None):
    lock = frozen.check_lock()
    now = now or datetime.now(frozen.CHINA)
    report = dict(version="PREFIX_ADMISSION_V4", baseline=BASE, protocol=lock["protocol"],
                  frozen_code_sha256=lock["code_sha256"], lock_sha256=frozen.digest(frozen.LOCK),
                  status="PENDING_SOURCE", exit_code=3, failures=[], products=[], hashes={},
                  received_score_days=0, sealed_score_days=0, proven_score_days=0, days={})
    def finish(reason):
        report["failures"].append(fail(reason))
        return report
    if not calendar_manifest or not calendar_manifest.is_file():
        return finish("INDEPENDENT_CALENDAR_NOT_DELIVERED")
    calendar_hash = frozen.digest(calendar_manifest)
    calendar = frozen.read(calendar_manifest)
    if not calendar.get("authority") or not calendar.get("version") or not calendar.get("sessions"):
        return finish("CALENDAR_AUTHORITY_OR_SESSIONS_MISSING")
    sessions = sorted(calendar["sessions"], key=lambda s:s["date"])
    previous = None
    records = []
    for s in sessions:
        r = frozen.calendar_record(s, lock)
        if (not s["source"] or s["previous"] >= s["date"] or
                (previous and (s["previous"] != previous["date"] or
                               frozen.stamp(previous["last_close"]) >= frozen.stamp(r["first_open"]))) or
                frozen.stamp(r["last_close"]).date().isoformat() != s["date"] or
                frozen.stamp(r["first_open"]).date().isoformat() < s["previous"]):
            raise ValueError("calendar chain/date bounds invalid")
        records.append(r)
        previous = r
    score_records = [r for r in records if r["scoring"]]
    if not score_records or not any(not r["scoring"] for r in records):
        return finish("CALENDAR_MUST_BRACKET_FROZEN_FIRST_DAY")
    first_day = score_records[0]["date"]
    report.update(fixed_first_day=first_day, calendar_version=calendar["version"], calendar_sha256=calendar_hash)
    if calendar.get("fixed_first_day") != first_day:
        return finish("FIXED_FIRST_DAY_ATTESTATION_MISMATCH")
    quote_frames, map_frames, supplied = [], [], []
    modes = set()
    for s,r in zip(sessions, records):
        day = s["date"]
        directory = bundle_root/day
        files = {name:directory/name for name in frozen.FILES}
        if not all(p.is_file() for p in files.values()):
            report["failures"].append(fail("DAILY_PACKAGE_NOT_DELIVERED", eob=day))
            break
        hashes = {name:frozen.digest(p) for name,p in files.items()}
        if hashes["sessions.json"] != calendar.get("session_sha256_by_day", {}).get(day):
            raise ValueError("independent calendar hash mismatch")
        if frozen.read(files["sessions.json"]) != s:
            raise ValueError("independent calendar content mismatch")
        if frozen.stamp(r["last_close"]) > now:
            report["failures"].append(fail("AWAITING_CLOSE", eob=day))
            break
        quotes, mapping = [pd.read_parquet(files[name]) for name in ("quotes.parquet","mapping.parquet")]
        for frame, columns in ((quotes, {"csymbol","trading_date","symbol","bob","eob","available_at","open","high","low","close"}),
                               (mapping, {"csymbol","trading_date","symbol","known_at"})):
            if columns-set(frame) or set(frame.csymbol) != set(lock["universe"]) or set(frame.trading_date.astype(str)) != {day}:
                raise ValueError("daily columns/universe/trading_date invalid")
        modes.add("RECORDED" if "decision_at" in quotes else "NOMINAL")
        report["hashes"][day] = hashes
        quote_frames.append(quotes)
        map_frames.append(mapping)
        supplied.append(s)
        report["days"][day] = {"received": True, "scoring": r["scoring"], "state":"RECEIVED"}
    if len(modes)>1 or (modes and next(iter(modes)) != calendar.get("decision_clock_mode")):
        raise ValueError("mixed or unattested decision clock modes")
    report["received_score_days"] = sum(v["scoring"] for v in report["days"].values())
    if quote_frames:
        quotes, mapping = pd.concat(quote_frames, ignore_index=True), pd.concat(map_frames, ignore_index=True)
        for s in lock["universe"]:
            result = inspect_product(s, quotes[quotes.csymbol.eq(s)].copy(), mapping[mapping.csymbol.eq(s)].copy(),
                                     supplied, first_day)
            report["products"].append(result)
            if result["first_failure"]:
                report["failures"].append(result["first_failure"])
    source_ok, source = provenance_status(bundle_root, calendar)
    report["source"] = source
    if study and study.exists():
        _, sealed = frozen.checked_days(study)
        for r in sealed:
            if r["date"] not in report["hashes"] or r["hashes"] != report["hashes"][r["date"]]:
                raise ValueError("SEALED_CONTENT_CONFLICT")
            report["days"][r["date"]]["sealed"] = True
            report["sealed_score_days"] += int(r["scoring"])
    for day, hashes in report["hashes"].items():
        if hashes != {f:frozen.digest(bundle_root/day/f) for f in frozen.FILES}:
            raise ValueError("INPUT_CHANGED_DURING_QA")
    if frozen.digest(calendar_manifest) != calendar_hash:
        raise ValueError("CALENDAR_CHANGED_DURING_QA")
    structural = any(p["first_failure"] for p in report["products"])
    if structural:
        report.update(status="QA_FAILED", exit_code=2)
    elif not report["failures"] and source_ok and first_day in report["days"]:
        report.update(status="PREFIX_QA_PASS", exit_code=0)
        report["proven_score_days"] = report["received_score_days"]
        for day in report["days"].values():
            day["state"] = "SEALED" if day.get("sealed") else "PREFIX_QA_PASS"
    report["technical_prefix_pass"] = bool(report["products"]) and not structural and not report["failures"]
    return report


def seal_passed(report, bundle_root, study):
    if report["exit_code"] != 0:
        raise ValueError("seal requires a source-verified complete prefix")
    # Snapshot every approved byte before the first mutation of the official study.
    with tempfile.TemporaryDirectory() as temp:
        stage = Path(temp)
        for day, hashes in report["hashes"].items():
            (stage/day).mkdir()
            for name, expected in hashes.items():
                shutil.copyfile(bundle_root/day/name, stage/day/name)
                if frozen.digest(stage/day/name) != expected:
                    raise ValueError("INPUT_CHANGED_AFTER_QA")
        for day, hashes in report["hashes"].items():
            frozen.ingest(study, stage/day)
            if frozen.read(study/"days"/day/"receipt.json")["hashes"] != hashes:
                raise ValueError("SEALED_RECEIPT_HASH_MISMATCH")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--calendar-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--study", type=Path)
    parser.add_argument("--seal", action="store_true")
    args = parser.parse_args()
    # Quality output must never reside inside staged inputs or the official study.
    if (args.output.resolve().is_relative_to(args.bundle_root.resolve()) or
            (args.study and args.output.resolve().is_relative_to(args.study.resolve()))):
        parser.error("quality directory must be separate from staging and study")
    try:
        result = preflight(args.bundle_root, args.calendar_manifest, args.study)
        if args.seal:
            if not args.study:
                raise ValueError("--seal requires an existing prepared --study")
            seal_passed(result, args.bundle_root, args.study)
            result = preflight(args.bundle_root, args.calendar_manifest, args.study)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result = dict(status="QA_FAILED", exit_code=2, failures=[fail(str(exc))], products=[])
    args.output.mkdir(parents=True, exist_ok=True)
    frozen.write(args.output/"prefix_quality.json", result)
    lock = frozen.check_lock()
    products = {p["csymbol"]:p for p in result["products"]}
    columns = ("csymbol","fixed_first_day","calendar_version","warmup_valid_m15","day1_expected_m1",
               "day1_received_any_leg_m1","day1_strict_valid_m1","first_bad_eob","reason","source_status","owner","due_at")
    with (args.output/"warmup_qualification.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for symbol in lock["universe"]:
            p = products.get(symbol, {})
            day = p.get("days", {}).get(result.get("fixed_first_day"), {})
            failure = p.get("first_failure") or {}
            writer.writerow(dict(csymbol=symbol, fixed_first_day=result.get("fixed_first_day","UNKNOWN"),
                                 calendar_version=result.get("calendar_version","UNKNOWN"),
                                 warmup_valid_m15=p.get("warmup_valid_m15","UNKNOWN"),
                                 day1_expected_m1=day.get("expected","UNKNOWN"),
                                 day1_received_any_leg_m1=day.get("received_any_leg","UNKNOWN"),
                                 day1_strict_valid_m1=day.get("strict_valid","UNKNOWN"),
                                 first_bad_eob=failure.get("eob") or "UNKNOWN",
                                 reason=failure.get("reason") or result["status"],
                                 source_status=result.get("source",{}).get("source_status","UNKNOWN"),
                                 owner="Codex 数据负责人",due_at="待核实"))
    print(json.dumps({k:result[k] for k in ("status","exit_code","failures")}, ensure_ascii=False))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
