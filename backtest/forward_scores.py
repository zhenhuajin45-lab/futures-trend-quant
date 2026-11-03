"""Post-replay scoring for the single sealed forward window."""
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean

import numpy as np

from .exit_rule_experiment import _event_exposure, _state_distribution
from .leg_scoring import score_reference_leg_events
from .metrics import future_48bar_3atr_first_touch, score_legacy_first_touch_metrics
from .reference_events import atr_directional_change_reference, score_confirmed_turn_events
from .trend_report import _predicted_state

GROUPS = ("S0", "EMA34_CONTROL", "A")
METRICS = ("direction_agreement", "trend_segment_coverage", "range_switches_per_product_day")


def distribution(values):
    if not values:
        return {"n": 0}
    q1, mid, q3 = np.quantile(values, [.25, .5, .75])
    return dict(n=len(values), mean=float(mean(values)), q25=float(q1), median=float(mid), q75=float(q3))


def moving_block_interval(by_day, length):
    """Each row is [paired net wins, complete events] across ALL symbols incl empty dates."""
    daily = np.asarray(by_day, dtype=int)
    if length > len(daily) or daily[:, 1].sum() == 0:
        return None
    rng = np.random.default_rng(20261103)
    starts = rng.integers(0, len(daily) - length + 1, size=(10_000, (len(daily) + length - 1)//length))
    draw = (starts[:, :, None] + np.arange(length)).reshape(10_000, -1)[:, :len(daily)]
    totals = daily[draw].sum(axis=1)
    valid = totals[:, 1] > 0
    if not valid.any():
        return None
    ratios = totals[valid, 0] / totals[valid, 1]
    return {"block": length, "replicates": int(valid.sum()), "interval": np.quantile(ratios, [.025, .975]).tolist(),
            "median": float(np.median(ratios))}


def space(anchor, high, low, atr, direction):
    up = max(0., high - anchor) / atr
    down = max(0., anchor - low) / atr
    return {"forward_atr": up if direction == "bull" else down,
            "adverse_atr": down if direction == "bull" else up, "range_atr": (high-low)/atr}


def label_counts(bars, atr, labels):
    counts = Counter(dict.fromkeys(("bull", "bear", "neither", "double_first_touch", "unscored"), 0))
    for i, v in enumerate(labels):
        if v is None or v:
            counts[{None: "unscored", 1: "bull", -1: "bear"}[v]] += 1
            continue
        both = any(b.high >= bars[i].close + 3*atr[i] and b.low <= bars[i].close - 3*atr[i]
                   for b in bars[i+1:i+49])
        counts["double_first_touch" if both else "neither"] += 1
    return counts


def summarize_product(replay, contracts, raw15, decision_clock=None):
    symbol = replay["csymbol"]
    bars, days = replay["bars"], replay["trading_dates"]
    atr = [r["atr14"] for r in replay["groups"]["S0"]]
    labels = future_48bar_3atr_first_touch(bars, atr)
    reference = atr_directional_change_reference(bars, atr)
    pairs, opportunities, summaries = {}, {}, {}
    for group in GROUPS:
        rows = replay["groups"][group]
        pred = [_predicted_state(r) for r in rows]
        bar_stats = score_legacy_first_touch_metrics(bars, atr, pred, labels, days)
        score = score_confirmed_turn_events(pred, reference.events)
        legs = score_reference_leg_events(pred, score.pop("events"))
        counts = _state_distribution(pred)
        first_prior = rows[0]["trend_decision"].get("prior_state")
        boundary_flip = first_prior in ("bull", "bear") and pred[0] in ("bull", "bear") and first_prior != pred[0]
        counts["flip_on_first_scored_bar"] = int(boundary_flip)
        direct = counts.get("direction_flip", 0)
        correct = {side: [sum(pred[i] == side for i, v in enumerate(labels) if v == label),
                          labels.count(label)] for side, label in (("bull", 1), ("bear", -1))}
        summary = {"bar_metrics": {k: bar_stats[k] for k in (*METRICS, "directional_reference_bars",
                   "trend_segment_bars","range_product_days","range_switches","evaluated_bars","eligible_trend_segments")},
                   "agreement_by_direction_correct_total": correct, "turns": score, "states": counts,
                   "flips_per_100_bars": direct * 100 / len(pred), "entry_funnel": {}}
        flow, blocked = Counter(), Counter()
        by_gate = {state: Counter() for state in ("open", "closed")}
        for e in legs["events"]:
            i, d = e["confirmation_index"], e["new_direction"]
            eid = e["event_id"]
            item = pairs.setdefault(eid, dict(event_id=eid, csymbol=symbol, direction=d,
                                              trading_date=days[i].isoformat(), eob=e["confirmation_eob"],
                                              groups={}, pivot_eob=e["pivot_eob"], pivot_atr=e["pivot_atr"],
                                              confirmation_atr=atr[i], warmup_valid=True, pivot_valid=True,
                                              confirmation_valid=True, label48_valid=e["observation_complete"]))
            exposure = _event_exposure(pred, e)
            proportions = {k: exposure[k]/exposure["leg_bars"] for k in
                           ("old_direction_bars","new_direction_bars","none_bars")}
            item["groups"][group] = {**exposure, "fractions":proportions, "delay_48": e["delay_bars"],
                                     "confirmed_pair_at_zero": e["leg_delay_bars"] == 0}
            follow = e["second_confirmation_index"]
            observed = {"eob": None, "space8": None}
            if follow is not None:
                observed["eob"] = bars[follow].eob.isoformat()
                after = bars[follow+1:follow+9]
                if len(after) == 8:
                    observed["space8"] = space(bars[follow].close, max(b.high for b in after),
                                               min(b.low for b in after), atr[follow], d)
            item["groups"][group]["after_observed_pair"] = observed
            future = bars[i+1:i+9]
            item["space8"] = (space(bars[i].close, max(b.high for b in future), min(b.low for b in future), atr[i], d)
                              if len(future) == 8 else None)
            item["already_moved_atr"] = abs(bars[i].close - bars[e["pivot_index"]].close)/atr[i]
        if group != "EMA34_CONTROL":
            for i, row in enumerate(rows):
                flow["complete_bars"] += 1
                en = row["entry_evaluation"]
                if en is None:
                    continue
                flow["authorized"] += 1
                gate_branch = by_gate["open" if en["gate_open"] else "closed"]
                gate_branch["authorized"] += 1
                for path in en["formed_paths"]:
                    gate_branch["formed_" + path] += 1
                    gate_branch["formed_with_anchor_" + path] += int(en["anchor_available"])
                flow["momentum_open"] += bool(en["gate_open"])
                flow["formed"] += bool(en["formed_paths"])
                flow["gate_open_and_formed"] += bool(en["gate_open"] and en["formed_paths"])
                flow["formed_with_anchor"] += bool(en["formed_paths"] and en["anchor_available"])
                if not en["accepted"]:
                    blocked[en["reason"]] += 1
                    continue
                flow["accepted_rows"] += 1
                gate_branch["accepted_" + en["path"]] += 1
                if en["evidence_id"] in replay.get("warmup_evidence", {}).get(group, ()):
                    flow["repeat_of_warmup"] += 1
                    continue
                key = (symbol, pred[i], en["path"], en["evidence_id"])
                op = opportunities.setdefault(key, dict(csymbol=symbol, direction=pred[i], path=en["path"],
                        evidence_id=en["evidence_id"], groups={}))
                if group in op["groups"]:
                    op["groups"][group]["repeat_rows"] += 1
                    continue
                ct = contracts[bars[i].eob]
                future = bars[i+1:i+9]
                raw = [raw15.get((ct, b.eob)) for b in future]
                anchor = raw15.get((ct, bars[i].eob))
                first_known = decision_clock[bars[i].eob] if decision_clock is not None else bars[i].eob
                datum = {"first_eob": bars[i].eob.isoformat(), "first_known_at": first_known.isoformat(),
                         "repeat_rows": 0, "actual_contract": ct,
                         "roll_in_window": any(contracts[b.eob] != ct for b in future),
                         "tail": len(future) < 8, "missing_actual_bar": anchor is None or None in raw,
                         "clock_straddles_next_bar": bool(future and future[0].bob < first_known),
                         "space8": None}
                if not (datum["tail"] or datum["missing_actual_bar"]):
                    datum["from_eob_space8"] = space(anchor.close, max(b.high for b in raw), min(b.low for b in raw),
                                                    atr[i], pred[i])
                    if not datum["clock_straddles_next_bar"]:
                        datum["space8"] = datum["from_eob_space8"]
                        datum["both_1atr_same_bar"] = any(b.high >= anchor.close+atr[i] and b.low <= anchor.close-atr[i]
                                                          for b in raw)
                op["groups"][group] = datum
                flow["first_opportunities"] += 1
            summary["entry_funnel"] = dict(flow)
            summary["entry_funnel_by_gate"] = {k:dict(v) for k,v in by_gate.items()}
            summary["blocked_at"] = dict(blocked)
        summaries[group] = summary
    for row in opportunities.values():
        row["membership"] = "shared" if len(row["groups"]) == 2 else "only_"+next(iter(row["groups"]))
    return {"labels": label_counts(bars, atr, labels), "groups": summaries}, list(pairs.values()), list(opportunities.values())


def decide(products, all_events, dates):
    full = [e for e in all_events if e["label48_valid"]]
    daily = {d: [0, 0] for d in dates}
    rescue = harm = 0
    by_symbol_day = defaultdict(lambda: [0, 0])
    for e in full:
        diff = int(e["groups"]["A"]["within_4_bars"]) - int(e["groups"]["S0"]["within_4_bars"])
        rescue += diff == 1
        harm += diff == -1
        for block in (daily[e["trading_date"]], by_symbol_day[(e["csymbol"], e["trading_date"])]):
            block[0] += diff
            block[1] += 1
    intervals = [moving_block_interval(list(daily.values()), n) for n in (5, 10)]
    groups = {}
    for g in GROUPS:
        scores = [p["groups"][g] for p in products.values()]
        bars = [s["bar_metrics"] for s in scores]
        weighted, equal, equal_counts, denominators = {}, {}, {}, {}
        for k, denominator in zip(METRICS, ("directional_reference_bars", "trend_segment_bars", "range_product_days")):
            n = sum(b[denominator] for b in bars)
            denominators[k] = n
            weighted[k] = sum(b[k]*b[denominator] for b in bars)/n if n else None
            observed = [b[k] for b in bars if b[denominator]]
            equal[k] = mean(observed) if observed else None
            equal_counts[k] = len(observed)
        delays = [e["groups"][g]["delay_48"] for e in full]
        weighted["delay"] = float(np.median(delays)) if delays else None
        denominators["delay"] = len(delays)
        symbol_delays = [[e["groups"][g]["delay_48"] for e in full if e["csymbol"] == s] for s in products]
        medians = [float(np.median(v)) for v in symbol_delays if v]
        equal["mean_product_median_delay"] = mean(medians) if medians else None
        equal_counts["mean_product_median_delay"] = len(medians)
        exp = Counter()
        for e in full:
            for key in ("old_direction_bars","new_direction_bars","none_bars","leg_bars"):
                exp[key] += e["groups"][g][key]
        ratios = {key: exp[key]/exp["leg_bars"] if exp["leg_bars"] else None
                  for key in ("old_direction_bars", "new_direction_bars", "none_bars")}
        timely = sum(e["groups"][g]["within_4_bars"] for e in full)
        zero = sum(e["groups"][g]["confirmed_pair_at_zero"] for e in full)
        total_bars = sum(s["states"]["bars"] for s in scores)
        flips = sum(s["states"].get("direction_flip", 0) for s in scores)
        boundary_flips = sum(s["states"].get("flip_on_first_scored_bar", 0) for s in scores)
        none_bars = sum(s["states"]["none_bars"] for s in scores)
        direction_counts = {}
        for direction in ("bull", "bear"):
            values = [s["agreement_by_direction_correct_total"][direction] for s in scores]
            count = sum(v[1] for v in values)
            direction_counts[direction] = {"correct": sum(v[0] for v in values), "total": count,
                                          "agreement": sum(v[0] for v in values)/count if count else None}
        passed = (all(v is not None for v in weighted.values()) and weighted["direction_agreement"] >= .6
                  and weighted["trend_segment_coverage"] >= .7 and weighted["range_switches_per_product_day"] <= 1
                  and weighted["delay"] <= 4)
        groups[g] = {"pool": weighted, "pool_denominators": denominators, "equal_product": equal,
                     "equal_product_count": equal_counts, "hard_gates_pass": passed,
                     "exposure": dict(exp), "exposure_fraction": ratios,
                     "timely": timely, "timely_rate": timely/len(full) if full else None,
                     "zero_delay": zero, "zero_delay_rate": zero/len(full) if full else None,
                     "directional_agreement": direction_counts, "direct_flips": flips,
                     "first_bar_flips_from_warmup": boundary_flips, "direct_flips_including_first_bar": flips + boundary_flips,
                     "flips_per_100_bars": 100*flips/total_bars if total_bars else None,
                     "none_bars": none_bars, "bars": total_bars, "none_rate": none_bars/total_bars if total_bars else None}
        pairs = [e["groups"][g]["after_observed_pair"] for e in full]
        groups[g]["after_observed_pair"] = {
            "no_pair_in_leg": sum(p["eob"] is None for p in pairs),
            "tail": sum(p["eob"] is not None and p["space8"] is None for p in pairs),
            **{k: distribution([p["space8"][k] for p in pairs if p["space8"] is not None])
               for k in ("forward_atr", "adverse_atr", "range_atr")}}
    a, s0 = (groups[k]["exposure"] for k in ("A","S0"))
    status = "INCONCLUSIVE"
    if len(full) >= 300 and intervals[0] and intervals[1] and len({e["csymbol"] for e in full}) == len(products):
        if (rescue > harm and a["old_direction_bars"] < s0["old_direction_bars"]
                and a["new_direction_bars"] > s0["new_direction_bars"]
                and intervals[0]["interval"][0] > 0 and intervals[1]["median"] > 0):
            status = "LOCAL_REPLICATED"
        elif rescue < harm and intervals[0]["interval"][1] < 0 and intervals[1]["median"] < 0:
            status = "NOT_REPLICATED"
    next_step = "CLOSE_EXIT_RESEARCH"
    if status == "LOCAL_REPLICATED":
        next_step = ("REVIEW_INCREMENTAL_VALUE_AND_OPPORTUNITIES" if groups["A"]["hard_gates_pass"]
                     else "CLOSE_EXIT_RESEARCH_REVIEW_DIRECTION_AUTHORIZATION")
    labels = sum((Counter(p["labels"]) for p in products.values()), Counter())
    return {"groups": groups, "local_status": status, "next_step": next_step, "label_counts": dict(labels),
            "complete": len(full), "censored": len(all_events)-len(full),
            "rescued": rescue,"harmed": harm, "delta": (rescue-harm)/len(full) if full else None,
            "bootstrap": intervals, "net_and_count_by_product_day": {"|".join(k):v for k,v in by_symbol_day.items()},
            "event_space8": {k: distribution([e["space8"][k] for e in all_events if e["space8"]])
                             for k in ("forward_atr","adverse_atr","range_atr")},
            "already_moved_atr": distribution([e["already_moved_atr"] for e in all_events])}


def summarize_opportunities(rows):
    result = {}
    for membership in ("shared","only_S0","only_A"):
        cohort = [r for r in rows if r["membership"] == membership]
        buckets = {}
        for group in ("S0","A"):
            values = [r["groups"][group] for r in cohort if group in r["groups"]]
            buckets[group] = {"n": len(values), "tail": sum(x["tail"] for x in values),
                              "missing": sum(x["missing_actual_bar"] for x in values),
                              "clock_straddle": sum(x["clock_straddles_next_bar"] for x in values),
                              "roll": sum(x["roll_in_window"] for x in values),
                              "double_1atr_same_bar":sum(bool(x.get("both_1atr_same_bar")) for x in values),
                              **{k:distribution([x["space8"][k] for x in values if x["space8"]])
                                 for k in ("forward_atr","adverse_atr","range_atr")}}
        result[membership] = {"unique_evidence":len(cohort),"by_first_known_time":buckets}
    return result
