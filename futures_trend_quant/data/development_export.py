from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from backtest.continuous import ContractBar, RollEvent, apply_additive_back_adjustment
from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.models import Bar
from futures_trend_quant.data.gm_bars import bars_from_records

from .gm_export import (
    ExportDataError,
    calculate_roll_differences,
    export_history_to_parquet,
    export_main_mapping_to_parquet,
)


@dataclass(frozen=True)
class ContractWindow:
    symbol: str
    start_time: datetime
    end_time: datetime


def contract_export_windows(mapping: Mapping[date, str]) -> list[ContractWindow]:
    """Build bounded contract windows and include the old leg on each roll date."""

    if not mapping:
        raise ValueError("main mapping cannot be empty")
    ordered = sorted(mapping.items())
    active: dict[str, list[date]] = {}
    for trading_date, symbol in ordered:
        active.setdefault(symbol, []).append(trading_date)
    next_roll_date: dict[str, date] = {}
    previous_symbol: str | None = None
    for trading_date, symbol in ordered:
        if previous_symbol is not None and symbol != previous_symbol:
            next_roll_date[previous_symbol] = trading_date
        previous_symbol = symbol
    windows = []
    for symbol, dates in active.items():
        first = min(dates)
        last = max(dates)
        end_date = max(last, next_roll_date.get(symbol, last))
        windows.append(
            ContractWindow(
                symbol=symbol,
                start_time=datetime.combine(first - timedelta(days=1), time(20, 0)),
                end_time=datetime.combine(end_date, time(16, 0)),
            )
        )
    return sorted(windows, key=lambda item: (item.start_time, item.symbol))


def assign_trading_date(timestamp: datetime, trading_dates: Sequence[date]) -> date:
    """Map a bar opening timestamp to the next applicable exchange trading date."""

    if not trading_dates:
        raise ValueError("trading date calendar cannot be empty")
    ordered = sorted(set(trading_dates))
    calendar_date = timestamp.date()
    index = (
        bisect_right(ordered, calendar_date)
        if timestamp.time() >= time(20, 0)
        else bisect_left(ordered, calendar_date)
    )
    if index >= len(ordered):
        raise ExportDataError(f"no trading date available for bar {timestamp.isoformat()}")
    return ordered[index]


def build_continuous_bars(
    *,
    csymbol: str,
    mapping: Mapping[date, str],
    contract_bars: Iterable[Bar],
) -> tuple[list[Bar], list[RollEvent], list[date]]:
    """Select daily main bars and create a stable, additively adjusted series."""

    trading_dates = sorted(mapping)
    rows: list[ContractBar] = []
    latest: dict[tuple[date, str], Bar] = {}
    for bar in sorted(contract_bars, key=lambda item: (item.eob, item.symbol)):
        try:
            trading_date = assign_trading_date(bar.bob, trading_dates)
        except ExportDataError:
            continue
        latest_key = (trading_date, bar.symbol)
        if latest_key not in latest or latest[latest_key].eob < bar.eob:
            latest[latest_key] = bar
        if mapping.get(trading_date) == bar.symbol:
            rows.append(ContractBar(trading_date, bar.symbol, bar))
    closes = {key: value.close for key, value in latest.items()}
    roll_rows = calculate_roll_differences(mapping, closes)
    rolls = [
        RollEvent(
            trading_date=item["trading_date"],
            old_contract=str(item["old_contract"]),
            new_contract=str(item["new_contract"]),
            price_difference=float(item["price_difference"]),
        )
        for item in roll_rows
    ]
    adjusted = apply_additive_back_adjustment(
        rows,
        mapping,
        rolls,
        continuous_symbol=csymbol,
    )
    adjusted_dates = [row.trading_date for row in rows]
    if len(adjusted) != len(adjusted_dates):
        raise AssertionError("adjusted bars and trading dates diverged")
    return adjusted, rolls, adjusted_dates


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_continuous(
    path: Path,
    bars: Sequence[Bar],
    trading_dates: Sequence[date],
) -> None:
    import pandas as pd

    rows = []
    for bar, trading_date in zip(bars, trading_dates):
        values = asdict(bar)
        values["trading_date"] = trading_date.isoformat()
        rows.append(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False, engine="pyarrow")


def export_development_dataset(
    *,
    output_root: Path,
    start_date: date,
    end_date: date,
) -> dict[str, object]:
    """Export mappings, actual-contract M1 and continuous bars for the universe."""

    import pandas as pd

    config = load_config(default_config_path())
    manifest_products: dict[str, object] = {}
    for product in config.products:
        stem = product.csymbol.replace(".", "_")
        mapping_path = output_root / "mapping" / f"{stem}.parquet"
        export_main_mapping_to_parquet(
            csymbol=product.csymbol,
            start_date=start_date,
            end_date=end_date,
            output_path=mapping_path,
        )
        mapping_frame = pd.read_parquet(mapping_path)
        mapping = {
            date.fromisoformat(str(row.trading_date)): str(row.symbol)
            for row in mapping_frame.itertuples(index=False)
        }
        raw_bars: list[Bar] = []
        raw_paths: list[Path] = []
        for window in contract_export_windows(mapping):
            raw_path = output_root / "raw" / f"{window.symbol.replace('.', '_')}.parquet"
            export_history_to_parquet(
                symbol=window.symbol,
                start_time=window.start_time,
                end_time=window.end_time,
                output_path=raw_path,
            )
            raw_paths.append(raw_path)
            raw_bars.extend(bars_from_records(pd.read_parquet(raw_path).to_dict("records")))
        adjusted, rolls, trading_dates = build_continuous_bars(
            csymbol=product.csymbol,
            mapping=mapping,
            contract_bars=raw_bars,
        )
        continuous_path = output_root / "continuous" / f"{stem}.parquet"
        _write_continuous(continuous_path, adjusted, trading_dates)
        manifest_products[product.csymbol] = {
            "mapping_rows": len(mapping),
            "actual_contracts": sorted({window.symbol for window in contract_export_windows(mapping)}),
            "continuous_m1_rows": len(adjusted),
            "rolls": [asdict(item) for item in rolls],
            "mapping_path": str(mapping_path.relative_to(output_root)),
            "mapping_sha256": _sha256(mapping_path),
            "continuous_path": str(continuous_path.relative_to(output_root)),
            "continuous_sha256": _sha256(continuous_path),
            "raw_files": [
                {"path": str(path.relative_to(output_root)), "sha256": _sha256(path)}
                for path in raw_paths
            ],
        }
    manifest = {
        "schema_version": 1,
        "purpose": "SPRINT2_DEVELOPMENT_DATASET",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "adjustment": "additive_new_minus_old_same_roll_date_close",
        "products": manifest_products,
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the frozen Sprint 2 GM development dataset")
    parser.add_argument("--token-env", default="GM_TOKEN")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2022, 10, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2024, 12, 31))
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        parser.error(f"missing GM token in environment variable {args.token_env}")
    from gm.api import set_token

    set_token(token)
    manifest = export_development_dataset(
        output_root=args.output_root,
        start_date=args.start,
        end_date=args.end,
    )
    print(json.dumps({
        "output_root": str(args.output_root),
        "products": len(manifest["products"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
