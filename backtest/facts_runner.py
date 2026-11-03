from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from futures_trend_quant.core.models import Bar

from .aggregation import SequentialAggregator
from .runner import FactRecord, ReplayBar, StepInput, StepOutput, StrategyStep


@dataclass
class FactsReplayResult:
    facts: list[FactRecord] = field(default_factory=list)
    m15_bars: list[Bar] = field(default_factory=list)
    trading_dates: list[object] = field(default_factory=list)
    blocked_at: list[str] = field(default_factory=list)


class FactsOnlyRunner:
    """Replay shared-core facts without fees, fills, positions or PnL.

    This runner is deliberately incapable of producing a performance report.
    It exists for the pre-registered trend layer, where verified broker fees
    and executable sizing are irrelevant.
    """

    def __init__(self, *, strategy_step: StrategyStep, history_limit: int = 1600) -> None:
        if strategy_step is None:
            raise ValueError("strategy_step is required")
        if history_limit < 1200:
            raise ValueError("facts replay history_limit must be at least 1200")
        self.strategy_step = strategy_step
        self.history_limit = history_limit

    def run(self, events: Iterable[ReplayBar]) -> FactsReplayResult:
        m5_agg = SequentialAggregator(5, "300s")
        m15_agg = SequentialAggregator(15, "900s")
        m1_history: dict[str, list[Bar]] = defaultdict(list)
        m5_history: dict[str, list[Bar]] = defaultdict(list)
        m15_history: dict[str, list[Bar]] = defaultdict(list)
        last_eob: dict[str, object] = {}
        result = FactsReplayResult()
        for event in sorted(events, key=lambda item: (item.bar.eob, item.bar.symbol)):
            bar = event.bar
            previous = last_eob.get(bar.symbol)
            if previous is not None and bar.eob <= previous:  # type: ignore[operator]
                raise ValueError(f"non-increasing bars for {bar.symbol}")
            last_eob[bar.symbol] = bar.eob
            m1_history[bar.symbol].append(bar)
            del m1_history[bar.symbol][:-self.history_limit]
            m5_closed = m5_agg.update(bar)
            if m5_closed is not None:
                m5_history[bar.symbol].append(m5_closed)
                del m5_history[bar.symbol][:-self.history_limit]
            m15_closed = m15_agg.update(bar)
            if m15_closed is None:
                continue
            m15_history[bar.symbol].append(m15_closed)
            del m15_history[bar.symbol][:-self.history_limit]
            output = self.strategy_step(
                StepInput(
                    symbol=bar.symbol,
                    trading_date=event.trading_date,
                    m15_bar=m15_closed,
                    m5_bars=tuple(m5_history[bar.symbol]),
                    m15_bars=tuple(m15_history[bar.symbol]),
                    position=None,
                    m1_bars=tuple(m1_history[bar.symbol]),
                    positions=(),
                )
            )
            if not isinstance(output, StepOutput):
                raise TypeError("strategy_step must return StepOutput")
            result.facts.append(FactRecord(bar.symbol, m15_closed.eob, dict(output.facts)))
            result.m15_bars.append(m15_closed)
            result.trading_dates.append(event.trading_date)
            if output.blocked_at:
                result.blocked_at.append(output.blocked_at)
            if output.signal is not None or output.close_reason is not None:
                raise ValueError("facts-only strategy must not emit executable intents")
        return result
