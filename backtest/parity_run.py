from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
import argparse
import json
from pathlib import Path

import pandas as pd

from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.facts.pipeline import trading_day_for_bar
from futures_trend_quant.core.risk import ContractSpec
from futures_trend_quant.data.gm_bars import bars_from_records

from .models import FeeSchedule
from .parity import compare_m15_facts
from .runner import OfflineRunner, ReplayBar, StepInput, StepOutput
from .strategy import SharedCoreStrategy


class StartGatedStrategy:
    def __init__(self, delegate: SharedCoreStrategy, first_eob: dict[str, datetime]) -> None:
        self.delegate = delegate
        self.first_eob = first_eob

    def __call__(self, event: StepInput) -> StepOutput:
        start = self.first_eob.get(event.symbol)
        if start is None or event.m15_bar.eob < start:
            return StepOutput(
                facts={
                    "symbol": event.symbol,
                    "eob": event.m15_bar.eob.isoformat(),
                },
                blocked_at="PARITY_PREHISTORY",
            )
        return self.delegate(event)


def _load_facts(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_parity(
    *,
    data_dir: Path,
    mapping_path: Path,
    gm_facts_path: Path,
    output_path: Path,
    mismatch_path: Path,
) -> dict[str, object]:
    config = load_config(default_config_path())
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    gm_facts = _load_facts(gm_facts_path)
    first_eob: dict[str, datetime] = {}
    for row in gm_facts:
        symbol = str(row["symbol"])
        timestamp = datetime.fromisoformat(str(row["eob"]))
        first_eob[symbol] = min(first_eob.get(symbol, timestamp), timestamp)

    events: list[ReplayBar] = []
    specs: dict[str, ContractSpec] = {}
    fees: dict[str, FeeSchedule] = {}
    csymbol_by_symbol: dict[str, str] = {}
    fee_raw = json.loads((Path(__file__).parents[1] / "config" / "fees.json").read_text(encoding="utf-8"))
    for csymbol, symbol in mapping.items():
        product = csymbol.split(".")[-1]
        structural = fee_raw["products"][product]
        frame = pd.read_parquet(data_dir / f"{csymbol.replace('.', '_')}.parquet")
        bars = bars_from_records(frame.to_dict("records"))
        events.extend(ReplayBar(bar, trading_day_for_bar(bar)) for bar in bars)
        csymbol_by_symbol[symbol] = csymbol
        specs[symbol] = ContractSpec(
            symbol=symbol,
            price_tick=float(structural["price_tick"]),
            multiplier=float(structural["multiplier"]),
            margin_ratio=0.10,
        )
        fees[product] = FeeSchedule(
            product=product,
            price_tick=float(structural["price_tick"]),
            multiplier=float(structural["multiplier"]),
            slippage_ticks=int(structural["slippage_ticks"]),
            verified=True,
        )

    core = SharedCoreStrategy(
        rules=config.raw["rules"],
        risk=config.raw["risk"],
        csymbol_by_symbol=csymbol_by_symbol,
        specs_by_symbol=specs,
        max_lots_by_product={},
    )
    result = OfflineRunner(
        fees=fees,
        strategy_step=StartGatedStrategy(core, first_eob),
        history_limit=1200,
    ).run(events)
    gm_identities = {(row["symbol"], row["eob"]) for row in gm_facts}
    offline = [
        dict(item.values)
        for item in result.facts
        if (item.symbol, str(item.values.get("eob"))) in gm_identities
    ]
    mismatches = compare_m15_facts(offline, gm_facts)
    by_key = Counter(item.key for item in mismatches)
    by_reason = Counter(item.reason for item in mismatches)
    summary = {
        "schema_version": 1,
        "purpose": "GM_OFFLINE_M15_FACTS_PARITY",
        "performance_interpretation_allowed": False,
        "gm_rows": len(gm_facts),
        "offline_rows": len(offline),
        "mismatch_count": len(mismatches),
        "mismatches_by_key": dict(sorted(by_key.items())),
        "mismatches_by_reason": dict(sorted(by_reason.items())),
        "acceptance_passed": not mismatches and len(offline) == len(gm_facts),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mismatch_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    mismatch_path.write_text(
        json.dumps([asdict(item) for item in mismatches], ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay GM M1 parquet and compare shared M15 facts")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--gm-facts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mismatches", type=Path, required=True)
    args = parser.parse_args()
    summary = run_parity(
        data_dir=args.data_dir,
        mapping_path=args.mapping,
        gm_facts_path=args.gm_facts,
        output_path=args.output,
        mismatch_path=args.mismatches,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["acceptance_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
