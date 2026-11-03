from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd

from futures_trend_quant.core.facts.pipeline import trading_day_for_bar
from futures_trend_quant.core.models import Bar
from futures_trend_quant.data.gm_bars import bars_from_records

from .aggregation import aggregate_bars
from .metrics import legacy_first_touch_metrics


def _eob_key(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return datetime.fromisoformat(str(value)).isoformat()


def _predicted_state(row: Mapping[str, object]) -> str:
    decision = row.get("trend_decision")
    if not isinstance(decision, Mapping):
        raise ValueError("fact row is missing trend_decision")
    state = decision.get("state")
    if not isinstance(state, Mapping):
        raise ValueError("trend_decision is missing state")
    value = str(state.get("trend_state", ""))
    if value not in {"bull", "bear", "none"}:
        raise ValueError(f"unsupported trend state: {value!r}")
    return value


def build_trend_report(
    facts: Iterable[Mapping[str, object]],
    m15_bars: Iterable[Bar],
    *,
    dataset_label: str,
    official_development: bool = False,
) -> dict[str, object]:
    """Build a post-run trend report without exposing future labels to strategy code."""

    bar_index = {(bar.symbol, _eob_key(bar.eob)): bar for bar in m15_bars}
    rows = sorted(
        (dict(row) for row in facts),
        key=lambda row: (str(row.get("csymbol")), _eob_key(row.get("eob"))),
    )
    if not rows:
        raise ValueError("trend report requires fact rows")

    observations: dict[str, list[tuple[Bar, float, str, date]]] = defaultdict(list)
    missing: list[tuple[str, str]] = []
    for row in rows:
        symbol = str(row.get("symbol", ""))
        csymbol = str(row.get("csymbol", ""))
        eob = _eob_key(row.get("eob"))
        bar = bar_index.get((symbol, eob))
        if bar is None:
            missing.append((symbol, eob))
            continue
        atr = row.get("atr14")
        if not isinstance(atr, (int, float)) or float(atr) <= 0:
            continue
        observations[csymbol].append(
            (bar, float(atr), _predicted_state(row), trading_day_for_bar(bar))
        )
    if missing:
        preview = ", ".join(f"{symbol}@{eob}" for symbol, eob in missing[:3])
        raise ValueError(f"{len(missing)} fact rows have no matching M15 bar: {preview}")
    if not observations:
        raise ValueError("no complete trend observations")

    per_product: dict[str, object] = {}
    pool_bars: list[Bar] = []
    pool_atr: list[float] = []
    pool_predicted: list[str] = []
    pool_dates: list[date] = []
    pool_symbols: list[str] = []
    for csymbol, items in sorted(observations.items()):
        items.sort(key=lambda item: item[0].eob)
        bars = [item[0] for item in items]
        atr = [item[1] for item in items]
        predicted = [item[2] for item in items]
        dates = [item[3] for item in items]
        per_product[csymbol] = legacy_first_touch_metrics(
            bars, atr, predicted, dates, symbols=[csymbol] * len(items)
        )
        pool_bars.extend(bars)
        pool_atr.extend(atr)
        pool_predicted.extend(predicted)
        pool_dates.extend(dates)
        pool_symbols.extend([csymbol] * len(items))

    all_dates = pool_dates
    if official_development:
        required = {
            "DCE.LH", "DCE.M", "CZCE.CF", "GFEX.LC", "CZCE.SA",
            "DCE.JD", "DCE.V", "DCE.EG", "SHFE.AU", "SHFE.AG",
            "CZCE.CJ", "CZCE.SR",
        }
        missing_products = sorted(required.difference(observations))
        if missing_products:
            raise ValueError(f"official development report is missing products: {missing_products}")
        if min(all_dates) > date(2023, 1, 3) or max(all_dates) < date(2024, 12, 31):
            raise ValueError("official development report does not cover the frozen 2023-2024 sample")
    return {
        "schema_version": 1,
        "purpose": "SPRINT2_LEGACY_FIRST_TOUCH_EVALUATION",
        "reference_definition": "LEGACY_FIRST_TOUCH_LABEL_CHANGE",
        "dataset_label": dataset_label,
        "report_status": "OFFICIAL_DEVELOPMENT" if official_development else "ENGINEERING_DRY_RUN",
        "preregistered_sample_interpretation_allowed": official_development,
        "performance_interpretation_allowed": False,
        "fact_rows": len(rows),
        "scored_input_rows": len(pool_bars),
        "first_trading_date": min(all_dates).isoformat(),
        "last_trading_date": max(all_dates).isoformat(),
        "products": sorted(observations),
        "pool": legacy_first_touch_metrics(
            pool_bars,
            pool_atr,
            pool_predicted,
            pool_dates,
            symbols=pool_symbols,
        ),
        "per_product": per_product,
    }


def _load_facts(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_m15_bars(data_dir: Path) -> list[Bar]:
    output: list[Bar] = []
    paths = [path for path in sorted(data_dir.glob("*.parquet")) if "_900s" not in path.stem]
    if not paths:
        raise ValueError(f"no M1 parquet files found in {data_dir}")
    for path in paths:
        frame = pd.read_parquet(path)
        bars = bars_from_records(frame.to_dict("records"))
        output.extend(aggregate_bars(bars, minutes=15, frequency="900s"))
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score frozen Sprint 2 trend metrics")
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--dataset-label", required=True)
    parser.add_argument("--official-development", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = build_trend_report(
        _load_facts(args.facts),
        _load_m15_bars(args.data_dir),
        dataset_label=args.dataset_label,
        official_development=args.official_development,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "rows": report["fact_rows"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
