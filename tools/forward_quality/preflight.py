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


def proof_history(bundle_root, proof_root, source_hash, lock_hash):
    history = []
    for path in sorted(proof_root.glob("*.json")):
        try:
            proof = frozen.read(path)
            if not {"lock_sha256","provenance_sha256","last_proven_day","hashes","fixed_first_day"} <= proof.keys():
                raise ValueError("incomplete proof receipt")
        except (ValueError, TypeError, AttributeError):
            history.append(dict(proof_sha256=frozen.digest(path), status="INVALID_CONTEXT",
                                last_proven_day=None, proven_score_days=0))
            continue
        status = "PROVEN"
        if (proof.get("snapshot_only") is not False or not proof.get("through_close")
                or frozen.stamp(proof["through_close"]) > datetime.now(frozen.CHINA)
                or frozen.stamp(proof["through_close"]).date().isoformat() != proof["last_proven_day"]
                or max(proof["hashes"], default="") != proof["last_proven_day"]):
            status = "INVALID_CONTEXT"
        else:
            for day, hashes in proof["hashes"].items():
                if (not all((bundle_root/day/f).is_file() for f in frozen.FILES) or
                        {f:frozen.digest(bundle_root/day/f) for f in frozen.FILES} != hashes):
                    status = "CONFLICT"
                    break
            if status == "PROVEN" and (proof["lock_sha256"] != lock_hash or proof["provenance_sha256"] != source_hash):
                status = "REVOKED"
        history.append(dict(proof_sha256=frozen.digest(path), status=status,
                            last_proven_day=proof["last_proven_day"],
                            proven_score_days=sum(day >= proof["fixed_first_day"] for day in proof["hashes"])))
    return history


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
    owner = str(source.get("owner", "")).strip().casefold()
    reviewer = str(source.get("independent_reviewer", "")).strip().casefold()
    if not owner or not reviewer or owner == reviewer:
        return False, {"source_status": "UNKNOWN", "reason": "INDEPENDENT_SOURCE_REVIEW_PENDING"}
    return True, {"source_status": "VERIFIED", "owner": source["owner"],
                  "independent_reviewer": source["independent_reviewer"],
                  "provenance_sha256": frozen.digest(path), "coverage":source.get("coverage", {})}


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


def preflight(bundle_root, calendar_manifest, study=None, now=None, proof_root=None):
    lock = frozen.check_lock()
    snapshot_only = now is not None
    now = now or datetime.now(frozen.CHINA)
    report = dict(version="PREFIX_ADMISSION_V5", baseline=BASE, protocol=lock["protocol"],
                  frozen_code_sha256=lock["code_sha256"], lock_sha256=frozen.digest(frozen.LOCK),
                  status="PENDING_SOURCE", exit_code=3, failures=[], products=[], hashes={},
                  received_score_days=0, sealed_score_days=0, proven_score_days=0, days={},
                  snapshot_only=snapshot_only, checked_at=now.isoformat(), historical_proofs=[])
    def finish(reason):
        report["failures"].append(fail(reason))
        if proof_root and not snapshot_only:
            source_file = bundle_root/"provenance.json"
            report["historical_proofs"] = proof_history(
                bundle_root, proof_root, frozen.digest(source_file) if source_file.is_file() else None,
                report["lock_sha256"])
            proven = [p for p in report["historical_proofs"] if p["status"] == "PROVEN"]
            report["proven_score_days"] = max((p["proven_score_days"] for p in proven), default=0)
            report["historically_proven_score_days"] = report["proven_score_days"]
            report["last_proven_day"] = max((p["last_proven_day"] for p in proven), default=None)
            if any(p["status"] in ("CONFLICT", "INVALID_CONTEXT") for p in report["historical_proofs"]):
                report.update(status="QA_FAILED", exit_code=2)
                report["failures"].append(fail("PROVEN_PREFIX_CONFLICT_OR_INVALID_CONTEXT"))
            elif any(p["status"] == "REVOKED" for p in report["historical_proofs"]):
                report["failures"].append(fail("PROVEN_PREFIX_SOURCE_REVOKED"))
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
    for record in records:
        if frozen.stamp(record["last_close"]) > now:
            report["days"][record["date"]] = dict(received=False, scoring=record["scoring"], state="AWAITING_CLOSE")
    for s,r in zip(sessions, records):
        day = s["date"]
        if frozen.stamp(r["last_close"]) > now:
            report["days"][day] = dict(received=False, scoring=r["scoring"], state="AWAITING_CLOSE")
            continue
        directory = bundle_root/day
        files = {name:directory/name for name in frozen.FILES}
        if not all(p.is_file() for p in files.values()):
            report["failures"].append(fail("DAILY_PACKAGE_NOT_DELIVERED", eob=day))
            report["days"][day] = dict(received=False, scoring=r["scoring"], state="BLOCKED")
            break
        hashes = {name:frozen.digest(p) for name,p in files.items()}
        if hashes["sessions.json"] != calendar.get("session_sha256_by_day", {}).get(day):
            raise ValueError("independent calendar hash mismatch")
        if frozen.read(files["sessions.json"]) != s:
            raise ValueError("independent calendar content mismatch")
        quotes, mapping = [pd.read_parquet(files[name]) for name in ("quotes.parquet","mapping.parquet")]
        for frame, columns in ((quotes, {"csymbol","trading_date","symbol","bob","eob","available_at","open","high","low","close"}),
                               (mapping, {"csymbol","trading_date","symbol","known_at"})):
            if columns-set(frame) or set(frame.csymbol) != set(lock["universe"]) or set(frame.trading_date.astype(str)) != {day}:
                raise ValueError("daily columns/universe/trading_date invalid")
        for symbol in lock["universe"]:
            allowed = set(frozen.expected_times(s["products"][symbol]))
            actual = set(map(frozen.stamp, quotes.loc[quotes.csymbol.eq(symbol), "eob"]))
            if actual - allowed:
                raise ValueError(f"DAILY_EOB_OUTSIDE_PRODUCT_SESSION {symbol} {min(actual-allowed)}")
        modes.add("RECORDED" if "decision_at" in quotes else "NOMINAL")
        report["hashes"][day] = hashes
        quote_frames.append(quotes)
        map_frames.append(mapping)
        supplied.append(s)
        report["days"][day] = {"received": True, "scoring": r["scoring"], "state":"RECEIVED"}
    if len(modes)>1 or (modes and next(iter(modes)) != calendar.get("decision_clock_mode")):
        raise ValueError("mixed or unattested decision clock modes")
    report["received_score_days"] = sum(v["scoring"] and v["received"] for v in report["days"].values())
    report["through_close"] = max((r["last_close"] for r in records if r["date"] in report["hashes"]), default=None)
    if quote_frames:
        quotes, mapping = pd.concat(quote_frames, ignore_index=True), pd.concat(map_frames, ignore_index=True)
        for s in lock["universe"]:
            result = inspect_product(s, quotes[quotes.csymbol.eq(s)].copy(), mapping[mapping.csymbol.eq(s)].copy(),
                                     supplied, first_day)
            report["products"].append(result)
            if result["first_failure"]:
                report["failures"].append(result["first_failure"])
    source_ok, source = provenance_status(bundle_root, calendar)
    coverage = source.get("coverage", {})
    required_fields = {"known_at","available_at","bob","eob"}
    if calendar.get("decision_clock_mode") == "RECORDED":
        required_fields.add("decision_at")
    if source_ok and supplied and (
            set(coverage.get("csymbols", [])) != set(lock["universe"])
            or coverage.get("from_day", "9999") > supplied[0]["date"]
            or coverage.get("through_day", "") < supplied[-1]["date"]
            or not required_fields.issubset(coverage.get("fields", []))
            or not coverage.get("chain_explanation")):
        source_ok = False
        source.update(source_status="UNKNOWN", reason="SOURCE_COVERAGE_UNVERIFIED")
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
    elif not report["failures"] and source_ok and first_day in report["hashes"]:
        report.update(status="PREFIX_QA_PASS", exit_code=0)
        report["proven_score_days"] = report["received_score_days"]
        for day in report["days"].values():
            if day["received"]:
                day["state"] = "SEALED" if day.get("sealed") else "PREFIX_QA_PASS"
    report["technical_prefix_pass"] = bool(report["products"]) and not structural and not report["failures"]
    report["warmup_pass"] = bool(report["products"]) and all(p["warmup_valid_m15"] >= 1200 for p in report["products"])
    report["first_day_closed"] = frozen.stamp(next(r["last_close"] for r in records if r["date"] == first_day)) <= now
    report["source_review_pass"] = source_ok
    report["project_approval_pass"] = False
    valid_days = []
    if source_ok and report["warmup_pass"]:
        for s in supplied:
            day = s["date"]
            if any(p["days"].get(day, {}).get("failures", 1) for p in report["products"]):
                break
            # Per-bucket errors are not part of minute counters.
            if any(p["first_failure"] and (p["first_failure"]["eob"] is None or
                   str(p["first_failure"]["eob"])[:10] <= day) for p in report["products"]):
                break
            valid_days.append(day)
    report["proven_score_days"] = sum(day >= first_day for day in valid_days)
    report["last_proven_day"] = valid_days[-1] if valid_days else None
    if proof_root and not snapshot_only:
        proof_root.mkdir(parents=True, exist_ok=True)
        report["historical_proofs"] = proof_history(bundle_root, proof_root,
                                                    source.get("provenance_sha256"), report["lock_sha256"])
        invalid_history = any(p["status"] in ("CONFLICT","INVALID_CONTEXT") for p in report["historical_proofs"])
        report["historically_proven_score_days"] = max(
            (p["proven_score_days"] for p in report["historical_proofs"] if p["status"] == "PROVEN"), default=0)
        report["proven_score_days"] = max(report["proven_score_days"], report["historically_proven_score_days"])
        if invalid_history:
            report.update(status="QA_FAILED", exit_code=2, proven_score_days=0, technical_prefix_pass=False)
            report["failures"].append(fail("PROVEN_PREFIX_CONFLICT_OR_REVOKED"))
        if not invalid_history and valid_days and any(day >= first_day for day in valid_days):
            proof = dict(lock_sha256=report["lock_sha256"], fixed_first_day=first_day,
                         last_proven_day=valid_days[-1], provenance_sha256=source["provenance_sha256"],
                         snapshot_only=False,
                         through_close=next(r["last_close"] for r in records if r["date"] == valid_days[-1]),
                         provenance_snapshot=frozen.read(bundle_root/"provenance.json"),
                         hashes={day:report["hashes"][day] for day in valid_days})
            from hashlib import sha256
            identity = sha256(json.dumps(proof,sort_keys=True).encode()).hexdigest()
            path = proof_root/(identity+".json")
            if not path.exists():
                with tempfile.NamedTemporaryFile(dir=proof_root, suffix=".pending", delete=False) as stream:
                    temporary = Path(stream.name)
                try:
                    frozen.write(temporary, proof)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
    return report


def approval_binding(report):
    return {k:report[k] for k in ("fixed_first_day","lock_sha256","calendar_sha256","hashes")} | {
        "through_day": max(report["hashes"]), "provenance_sha256":report["source"]["provenance_sha256"]}


def seal_passed(report, bundle_root, study, approval=None):
    if report["exit_code"] != 0:
        raise ValueError("seal requires a source-verified complete prefix")
    if report.get("snapshot_only"):
        raise ValueError("historical as_of snapshot cannot authorize seal")
    if not approval or approval.get("binding") != approval_binding(report):
        raise ValueError("PROJECT_APPROVAL_MISSING_OR_MISMATCH")
    identities = [str(approval.get(k,"")).strip().casefold() for k in ("project_owner","independent_reviewer")]
    if (not all(identities) or len(set(identities)) != 2 or
            identities[1] != report["source"]["independent_reviewer"].strip().casefold() or
            approval.get("decision") != "APPROVE_SEAL" or not approval.get("signed_at") or not approval.get("approval_ref")):
        raise ValueError("PROJECT_APPROVAL_IDENTITY_OR_SIGNATURE_INVALID")
    signed = frozen.stamp(approval["signed_at"])
    if (signed > datetime.now(frozen.CHINA) or signed < frozen.stamp(frozen.check_lock()["freeze_time"])
            or signed < frozen.stamp(report["through_close"])):
        raise ValueError("PROJECT_APPROVAL_TIME_INVALID")
    evidence = (bundle_root/approval["approval_ref"]).resolve()
    if (not evidence.is_relative_to(bundle_root.resolve()) or not evidence.is_file()
            or frozen.digest(evidence) != approval.get("approval_sha256")):
        raise ValueError("PROJECT_APPROVAL_EVIDENCE_UNVERIFIED")
    if frozen.digest(bundle_root/"provenance.json") != report["source"]["provenance_sha256"]:
        raise ValueError("SOURCE_CHANGED_AFTER_QA")
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
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--as-of", help="Historical QA snapshot only, never usable with --seal")
    args = parser.parse_args()
    if args.as_of and args.seal:
        parser.error("--as-of cannot be combined with --seal")
    # Quality output must never reside inside staged inputs or the official study.
    if (args.output.resolve().is_relative_to(args.bundle_root.resolve()) or
            (args.study and args.output.resolve().is_relative_to(args.study.resolve()))):
        parser.error("quality directory must be separate from staging and study")
    result = None
    try:
        result = preflight(args.bundle_root, args.calendar_manifest, args.study,
                           frozen.stamp(args.as_of) if args.as_of else None, args.output/"proofs")
        if args.seal:
            if not args.study:
                raise ValueError("--seal requires an existing prepared --study")
            seal_passed(result, args.bundle_root, args.study, frozen.read(args.approval) if args.approval else None)
            result = preflight(args.bundle_root, args.calendar_manifest, args.study, proof_root=args.output/"proofs")
            result["project_approval_pass"] = True
            result["approval_sha256"] = frozen.digest(args.approval)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        if result is not None:
            result.update(status="QA_FAILED", exit_code=2, project_approval_pass=False)
            result["failures"].append(fail(str(exc)))
        else:
            result = dict(status="QA_FAILED", exit_code=2, failures=[fail(str(exc))], products=[])
        source_file = args.bundle_root/"provenance.json"
        result["historical_proofs"] = [] if args.as_of else proof_history(
            args.bundle_root, args.output/"proofs", frozen.digest(source_file) if source_file.is_file() else None,
            frozen.digest(frozen.LOCK))
        result["proven_score_days"] = max(
            (p["proven_score_days"] for p in result["historical_proofs"] if p["status"] == "PROVEN"), default=0)
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
