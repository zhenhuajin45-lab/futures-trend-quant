from __future__ import annotations

import argparse
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from futures_trend_quant.config import default_config_path
from futures_trend_quant.data.development_export import validate_dataset_manifest
from futures_trend_quant.data.gm_bars import bars_from_records

from .aggregation import aggregate_bars


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dataset_id(*parts: str) -> str:
    return "gm-rolling-180d-" + sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


def continuous_quality(frame: pd.DataFrame) -> dict[str, object]:
    required = {"symbol", "eob", "open", "high", "low", "close", "trading_date"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"continuous frame is missing columns: {missing}")
    timestamps = pd.to_datetime(frame["eob"], errors="coerce")
    duplicate_rows = int(frame.duplicated(subset=["symbol", "eob"]).sum())
    invalid_time = int(timestamps.isna().sum())
    non_increasing = 0
    observed_gaps = 0
    largest_gap_seconds = 0.0
    for _, group in frame.assign(_eob=timestamps).groupby("symbol", sort=False):
        values = group["_eob"].dropna().sort_values()
        differences = values.diff().dropna().dt.total_seconds()
        non_increasing += int((differences <= 0).sum())
        gaps = differences[differences > 60]
        observed_gaps += int(len(gaps))
        if len(gaps):
            largest_gap_seconds = max(largest_gap_seconds, float(gaps.max()))
    open_values = pd.to_numeric(frame["open"], errors="coerce")
    high_values = pd.to_numeric(frame["high"], errors="coerce")
    low_values = pd.to_numeric(frame["low"], errors="coerce")
    close_values = pd.to_numeric(frame["close"], errors="coerce")
    invalid_ohlc = (
        open_values.isna()
        | high_values.isna()
        | low_values.isna()
        | close_values.isna()
        | (open_values <= 0)
        | (high_values <= 0)
        | (low_values <= 0)
        | (close_values <= 0)
        | (high_values < pd.concat([open_values, low_values, close_values], axis=1).max(axis=1))
        | (low_values > pd.concat([open_values, high_values, close_values], axis=1).min(axis=1))
    )
    timezone_values = sorted({
        str(value.tzinfo) for value in timestamps.dropna().head(1000) if value.tzinfo is not None
    })
    return {
        "rows": int(len(frame)),
        "duplicate_symbol_eob_rows": duplicate_rows,
        "invalid_eob_rows": invalid_time,
        "non_increasing_rows_after_sort": non_increasing,
        "invalid_ohlc_rows": int(invalid_ohlc.sum()),
        "null_trading_date_rows": int(frame["trading_date"].isna().sum()),
        "observed_clock_gaps_gt_60s": observed_gaps,
        "largest_observed_clock_gap_seconds": largest_gap_seconds,
        "timezone_samples": timezone_values,
        "missing_minute_bars": None,
        "missing_minute_assessment": "NOT_ASSERTED_WITHOUT_VERSIONED_EXCHANGE_SESSION_CALENDAR",
    }


def build_dataset_evidence(
    root: Path,
    *,
    facts_path: Path,
    code_sha: str,
    generation_command: str,
) -> dict[str, object]:
    manifest = validate_dataset_manifest(root)
    manifest_path = root / "manifest.json"
    strategy_path = default_config_path()
    fees_path = strategy_path.parent / "fees.json"
    manifest_sha = _sha256(manifest_path)
    strategy_sha = _sha256(strategy_path)
    fees_sha = _sha256(fees_path)
    facts_sha = _sha256(facts_path)
    products = manifest.get("products")
    if not isinstance(products, Mapping):
        raise ValueError("manifest products must be a mapping")
    output_products: dict[str, object] = {}
    total_m1 = 0
    total_m15 = 0
    total_mapping = 0
    for csymbol, raw_product in sorted(products.items()):
        if not isinstance(raw_product, Mapping):
            raise ValueError(f"invalid product manifest {csymbol}")
        continuous_path = root / str(raw_product["continuous_path"])
        mapping_path = root / str(raw_product["mapping_path"])
        continuous = pd.read_parquet(continuous_path)
        mapping = pd.read_parquet(mapping_path)
        quality = continuous_quality(continuous)
        bars = bars_from_records(continuous.to_dict("records"))
        m15_rows = len(aggregate_bars(bars, minutes=15, frequency="900s"))
        mapping_date_column = "trading_date" if "trading_date" in mapping.columns else "trade_date"
        mapping_duplicate_dates = int(mapping.duplicated(subset=[mapping_date_column]).sum())
        mapping_null_symbols = int(mapping["symbol"].isna().sum())
        total_m1 += int(len(continuous))
        total_m15 += m15_rows
        total_mapping += int(len(mapping))
        output_products[str(csymbol)] = {
            "actual_contracts": list(raw_product.get("actual_contracts", [])),
            "rolls": list(raw_product.get("rolls", [])),
            "mapping_rows": int(len(mapping)),
            "mapping_duplicate_trading_dates": mapping_duplicate_dates,
            "mapping_null_symbols": mapping_null_symbols,
            "continuous_m1_rows": int(len(continuous)),
            "aggregated_m15_rows": m15_rows,
            "quality": quality,
            "files": {
                "mapping_sha256": str(raw_product["mapping_sha256"]),
                "continuous_sha256": str(raw_product["continuous_sha256"]),
                "raw_contract_sha256": [
                    {
                        "contract_file": Path(str(item["path"])).stem,
                        "sha256": str(item["sha256"]),
                    }
                    for item in raw_product.get("raw_files", [])
                ],
            },
        }
    facts_rows = sum(1 for line in facts_path.read_text(encoding="utf-8").splitlines() if line.strip())
    identity = dataset_id(manifest_sha, strategy_sha, facts_sha, code_sha)
    return {
        "schema_version": 2,
        "purpose": "SPRINT2_ROLLING_180D_REPRODUCIBILITY_EVIDENCE",
        "dataset_id": identity,
        "report_status": "ROLLING_180D_RESEARCH",
        "independent_validation": False,
        "raw_market_data_committed": False,
        "manifest_integrity_validation": "PASS",
        "code_sha": code_sha,
        "generation_command": generation_command,
        "source": {
            "provider": "GM",
            "sdk_version": version("gm"),
            "data_kind": "actual-contract M1 plus daily main mapping",
            "start_date": manifest.get("start_date"),
            "end_date": manifest.get("end_date"),
            "timezone": "Asia/Shanghai or explicit +08:00 stored in bar timestamps",
            "trading_date_assignment": "night bars with bob>=20:00 map to next available mapping trading date",
        },
        "continuous_adjustment": {
            "mode": str(manifest.get("adjustment")),
            "price_difference_sign": "new_contract_close - old_contract_close",
            "application": "from roll trading date onward subtract cumulative price difference",
            "real_time_gm_adjusted_series_connected": False,
        },
        "hashes": {
            "local_manifest_sha256": manifest_sha,
            "strategy_config_sha256": strategy_sha,
            "fees_config_sha256": fees_sha,
            "baseline_facts_sha256": facts_sha,
        },
        "totals": {
            "products": len(output_products),
            "mapping_rows": total_mapping,
            "continuous_m1_rows": total_m1,
            "aggregated_m15_rows": total_m15,
            "complete_m15_fact_rows": facts_rows,
            "actual_contracts": sum(len(item["actual_contracts"]) for item in output_products.values()),
            "rolls": sum(len(item["rolls"]) for item in output_products.values()),
        },
        "products": output_products,
        "limitations": [
            "The original parquet files remain local and Git-ignored.",
            "Missing M1 bars are not asserted without a versioned exchange session calendar; raw clock-gap counts include scheduled breaks and market closures.",
            "The rolling window has already been used for baseline and ablation research and is not an independent holdout.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build sanitized rolling-dataset evidence")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--generation-command", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = build_dataset_evidence(
        args.data_root,
        facts_path=args.facts,
        code_sha=args.code_sha,
        generation_command=args.generation_command,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "dataset_id": report["dataset_id"], "totals": report["totals"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
