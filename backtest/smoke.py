from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
from pathlib import Path

from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.core.models import Bar
from futures_trend_quant.core.risk import ContractSpec

from .metrics import blocked_distribution, trade_metrics
from .models import FeeSchedule
from .runner import OfflineRunner, ReplayBar
from .strategy import SharedCoreStrategy


def synthetic_events(count: int = 4_500) -> list[ReplayBar]:
    """Create deterministic M1 data for an engineering-only replay smoke test."""

    start = datetime(2026, 1, 5, 9, 0)
    output: list[ReplayBar] = []
    previous = 850.0
    for index in range(count):
        bob = start + timedelta(minutes=index)
        trend = index * 0.002
        wave = math.sin(index / 85.0) * 2.2 + math.sin(index / 13.0) * 0.35
        close = 850.0 + trend + wave
        open_price = previous
        high = max(open_price, close) + 0.12
        low = min(open_price, close) - 0.12
        bar = Bar(
            symbol="SHFE.AUSYNTH",
            frequency="60s",
            bob=bob,
            eob=bob + timedelta(minutes=1),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=1_000,
            amount=close * 1_000,
            position=10_000,
            pre_close=previous,
        )
        output.append(ReplayBar(bar, bar.eob.date()))
        previous = close
    return output


def run_smoke() -> dict[str, object]:
    config = load_config(default_config_path())
    symbol = "SHFE.AUSYNTH"
    strategy = SharedCoreStrategy(
        rules=config.raw["rules"],
        risk=config.raw["risk"],
        csymbol_by_symbol={symbol: "SHFE.AU"},
        specs_by_symbol={symbol: ContractSpec(symbol, 0.02, 1000, 0.12)},
        max_lots_by_product={"AUSYNTH": 10},
    )
    runner = OfflineRunner(
        fees={
            "AUSYNTH": FeeSchedule(
                product="AUSYNTH",
                multiplier=1000,
                price_tick=0.02,
                slippage_ticks=1,
                verified=True,
            )
        },
        strategy_step=strategy,
    )
    result = runner.run(synthetic_events())
    complete_facts = [item for item in result.facts if "trend_decision" in item.values]
    metrics = trade_metrics(result.trades)
    metrics = {
        key: (None if isinstance(value, float) and not math.isfinite(value) else value)
        for key, value in metrics.items()
    }
    return {
        "purpose": "ENGINEERING_SMOKE_ONLY",
        "performance_interpretation_allowed": False,
        "initial_cash": config.initial_cash,
        "m1_bars": 4_500,
        "m15_callbacks": len(result.facts),
        "complete_m15_facts": len(complete_facts),
        "blocked_at": blocked_distribution(result.blocked_at),
        "trade_metrics": metrics,
        "fills": len(result.fills),
        "open_position_stacks": {
            symbol: len(items) for symbol, items in result.open_positions.items()
        },
        "runtime_csymbols": sorted(strategy.runtime.trends),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a deterministic shared-core smoke backtest")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_smoke()
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
