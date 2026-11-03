"""Compare archived GM series and V3 as-of qualification without substituting timestamps."""
import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path

import pandas as pd

from futures_trend_quant.data.development_export import assign_trading_date, validate_dataset_manifest
from futures_trend_quant.data.gm_export import ExportDataError
from .asof_roll import build_asof_minutes


def prefix_probe(csymbol, raw, mapping, roll, day):
    """Future price+publication perturbation on genuine two-leg M1, with fixture clocks."""
    from futures_trend_quant.config import default_config_path, load_config
    from futures_trend_quant.data.gm_bars import bar_from_mapping
    from .exit_experiment_strategy import ExitExperimentFactsStrategy
    from .facts_runner import FactsOnlyRunner
    from .runner import ReplayBar

    day_rows = raw[raw.trading_date == day]
    cutoff = day_rows.eob.max() - pd.Timedelta(minutes=45)
    stamps = raw.loc[raw.eob <= cutoff,"eob"].drop_duplicates().sort_values()
    start = stamps.iloc[-4300]
    quotes = raw[(raw.eob>=start)&(raw.trading_date<=day)].assign(available_at=lambda f:f.eob)
    maps = mapping[mapping.trading_date.isin(quotes.trading_date.unique())].copy()
    altered = quotes.copy()
    altered.loc[(altered.bob>=cutoff)&(altered.symbol==roll["new_contract"]),
                ["open","high","low","close"]] += 9
    late = dict(trading_date=day, symbol=roll["old_contract"], known_at=cutoff+pd.Timedelta(minutes=1))
    revised = pd.concat([maps,pd.DataFrame([late])], ignore_index=True)
    orig,_ = build_asof_minutes(csymbol,maps,quotes)
    changed,_ = build_asof_minutes(csymbol,revised,altered)
    append_prefix,_ = build_asof_minutes(csymbol,revised[revised.known_at<=cutoff],altered[altered.available_at<=cutoff])
    def prefix(frame):
        return frame[frame.eob<=cutoff].reset_index(drop=True)
    pd.testing.assert_frame_equal(prefix(orig),prefix(changed))
    pd.testing.assert_frame_equal(prefix(orig),append_prefix)
    def states(frame):
        replay = FactsOnlyRunner(strategy_step=ExitExperimentFactsStrategy(
            "S0",load_config(default_config_path()).raw["rules"],{csymbol:csymbol}
        )).run(ReplayBar(bar_from_mapping(r),r["trading_date"])
               for r in frame[frame.eligible].to_dict("records"))
        return [dict(rec.values) for rec in replay.facts if rec.eob<=cutoff and "trend_decision" in rec.values]
    orig_facts = states(orig)
    assert orig_facts and states(changed)==states(append_prefix)==orig_facts
    return {
        "fixture_clocks": True, "trading_date":day, "cutoff_eob":cutoff,
        "closed_m1_prefix_rows":len(append_prefix), "fact_state_and_entry_prefix_rows":len(orig_facts),
        "technical_candidate_prefix_count":sum(bool((r.get("entry_evaluation") or {}).get("accepted")) for r in orig_facts),
        "full_vs_later_close_and_mapping_changed":"IDENTICAL_PREFIX",
        "full_vs_append_prefix":"IDENTICAL_PREFIX", "ineligible_m1_in_prefix":int((~append_prefix.eligible).sum()),
    }


def product_audit(root, csymbol, meta):
    mapping = pd.read_parquet(root / meta["mapping_path"])
    calendar = sorted(pd.to_datetime(mapping.trading_date).dt.date)
    raw = pd.concat([pd.read_parquet(root / f["path"]) for f in meta["raw_files"]], ignore_index=True)
    # Reproduce the archived calendar assignments to isolate the roll convention.
    # Only date and the >=20:00 boundary enter the assignment.
    date_map = {}
    for stamp in raw.bob:
        key = (stamp.date(), stamp.hour >= 20)
        if key not in date_map:
            try:
                date_map[key] = assign_trading_date(stamp, calendar)
            except ExportDataError:
                date_map[key] = None
    raw["trading_date"] = [date_map[(s.date(), s.hour >= 20)] for s in raw.bob]
    raw = raw[raw.trading_date.notna()].copy()
    if "known_at" in mapping or "available_at" in raw:
        raise ValueError("Archived audit is for legacy schema only")
    strict, _ = build_asof_minutes(csymbol, mapping.assign(known_at=pd.NaT), raw.assign(available_at=pd.NaT))
    if strict.eligible.any():
        raise ValueError("unknown availability unexpectedly qualified")
    # A separate, counterfactual clock fixture to exercise the real two-leg *prices*.
    # Neither timestamp assignment is presented as observed historical knowledge.
    starts = raw.groupby("trading_date").bob.min()
    synthetic_maps = mapping.assign(known_at=mapping.trading_date.map(starts))
    exercise, events = build_asof_minutes(csymbol, synthetic_maps, raw.assign(available_at=raw.eob))
    legacy = pd.read_parquet(root / meta["continuous_path"])
    joined = legacy[["eob","close"]].merge(exercise[["eob","close","eligible"]], how="left", on="eob",
                                          suffixes=("_legacy","_exercise"), validate="one_to_one")
    offsets = {(str(event["trading_date"]),event["new_contract"]):event for event in events}
    roll_rows = []
    for roll in meta["rolls"]:
        day = date.fromisoformat(roll["trading_date"])
        old, new = roll["old_contract"], roll["new_contract"]
        legs = raw[(raw.trading_date == day) & raw.symbol.isin([old,new])]
        distinct = legs.groupby("eob").size()
        old_times = set(legs.loc[legs.symbol == old, "eob"])
        new_times = set(legs.loc[legs.symbol == new, "eob"])
        previous = calendar[calendar.index(day)-1]
        previous_legs = raw[(raw.trading_date == previous) & (raw.eob.dt.hour==15)
                            & (raw.eob.dt.minute==0) & raw.symbol.isin([old,new])]
        fixture = offsets.get((str(day),new))
        end_gap = roll["price_difference"]
        row = {
            "trading_date":day.isoformat(), "old_contract":old, "new_contract":new,
            "mapping_known_at":"UNKNOWN", "old_available_at":"UNKNOWN", "new_available_at":"UNKNOWN",
            "strict_effective_bar":None, "strict_cumulative_offset":None, "strict_status":"DATA_BLOCKED",
            "contains_night_session":bool((legs.bob.dt.date != day).any()),
            "previous_close_missing_legs":sorted({old,new} - set(previous_legs.symbol)),
            "roll_day_single_leg_minutes":int((distinct==1).sum()),
            "roll_day_synchronous_minutes":len(old_times & new_times),
            "legacy_same_day_close_difference":end_gap,
            "legacy_old_close_eob":max(old_times).isoformat(),
            "legacy_new_close_eob":max(new_times).isoformat(),
            "legacy_offset_first_eob":min(new_times).isoformat(),
            "known_clock_fixture":None if fixture is None else {
                k:v for k,v in fixture.items() if k not in {"old_raw_close","new_raw_close"}
            },
        }
        roll_rows.append(row)
    # Fixed examples represent night, day-only and genuinely asynchronous legs.
    exemplar_dates={"CZCE.CF":"2026-08-14","CZCE.CJ":"2026-08-11","DCE.EG":"2026-08-24"}
    exemplar=next((r for r in meta["rolls"] if r["trading_date"]==exemplar_dates.get(csymbol)), None)
    probe = prefix_probe(csymbol,raw,synthetic_maps,exemplar,date.fromisoformat(exemplar["trading_date"])) if exemplar else None
    return {
        "legacy_m1":len(legacy), "mapping_product_days":len(calendar),
        "strict_eligible_m1":int(strict.eligible.sum()), "strict_lost_legacy_m1":len(legacy),
        "strict_observed_clock_status":dict(Counter(strict.reason)),
        "source_clocks_without_availability":int(raw.eob.nunique()),
        "clock_fixture_legacy_m1_compared":int(joined.eligible.eq(True).sum()),
        "clock_fixture_close_diff_gt_1e_8":int(((joined.close_legacy-joined.close_exercise).abs()>1e-8).sum()),
        "clock_fixture_legacy_m1_without_eligible_peer":int((~joined.eligible.eq(True)).sum()),
        "rolls":roll_rows,"prefix_probe":probe,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root",type=Path,required=True)
    parser.add_argument("--report",type=Path,required=True)
    args = parser.parse_args()
    manifest = validate_dataset_manifest(args.data_root)
    products = {cs:product_audit(args.data_root,cs,meta) for cs,meta in manifest["products"].items()}
    metric_keys = ("legacy_m1","mapping_product_days","strict_lost_legacy_m1","strict_eligible_m1",
                   "clock_fixture_legacy_m1_compared","clock_fixture_close_diff_gt_1e_8",
                   "clock_fixture_legacy_m1_without_eligible_peer")
    rows = [row for item in products.values() for row in item["rolls"]]
    report = {
        "purpose":"ARCHIVED_GM_180D_ROLL_KNOWLEDGE_AUDIT",
        "strict_status":"DATA_BLOCKED",
        "legacy_version":manifest["adjustment"], "asof_version":"ASOF_SYNCHRONOUS_V1",
        "clock_fixture":"Engineering only. Assume mapping known at first observed bob of trading date, quotes at eob.",
        "coverage":{k:sum(v[k] for v in products.values()) for k in metric_keys},
        "affected_roll_product_days":len(rows), "unknown_mapping_publications":len(rows),
        "strict_validity":{"confirmation":0,"future_48_label":0,"pivot_to_confirmation":0,
                          "pivot_atr_history":0,"strategy_warmup":0},
        "representative_cases":{key:next((r["new_contract"]+"@"+r["trading_date"] for r in rows if pred(r)),None)
                                for key,pred in {
                                     "night":lambda r:r["contains_night_session"],
                                     "day_only":lambda r:not r["contains_night_session"],
                                     "missing_previous_leg":lambda r:bool(r["previous_close_missing_legs"]),
                                     "within_roll_day_single_leg":lambda r:r["roll_day_single_leg_minutes"]>0,
                                }.items()},
        "products":products,
    }
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2,default=str)+"\n",encoding="utf-8")
    print(json.dumps({"coverage":report["coverage"], "examples":report["representative_cases"]}))


if __name__ == "__main__":
    main()
