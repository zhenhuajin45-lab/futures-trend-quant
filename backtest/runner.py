from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Mapping, Protocol

from futures_trend_quant.core.models import Bar

from .aggregation import SequentialAggregator
from .execution import match_exit_in_bar, open_position
from .models import FeeSchedule, Fill, Position, SignalIntent, Trade


@dataclass(frozen=True)
class ReplayBar:
    bar: Bar
    trading_date: date


@dataclass(frozen=True)
class StepInput:
    symbol: str
    trading_date: date
    m15_bar: Bar
    m5_bars: tuple[Bar, ...]
    m15_bars: tuple[Bar, ...]
    position: Position | None


@dataclass(frozen=True)
class StepOutput:
    signal: SignalIntent | None = None
    facts: Mapping[str, object] = field(default_factory=dict)
    blocked_at: str | None = None


class StrategyStep(Protocol):
    def __call__(self, event: StepInput) -> StepOutput: ...


@dataclass(frozen=True)
class FactRecord:
    symbol: str
    eob: object
    values: Mapping[str, object]


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    facts: list[FactRecord] = field(default_factory=list)
    blocked_at: list[str] = field(default_factory=list)
    open_positions: dict[str, Position] = field(default_factory=dict)


class OfflineRunner:
    """Deterministic M1 event loop with injected strategy core.

    The runner deliberately refuses to start when fee schedules are not marked
    verified.  This prevents a seemingly successful PnL report from being
    produced with placeholder costs.
    """

    def __init__(
        self,
        *,
        fees: Mapping[str, FeeSchedule],
        strategy_step: StrategyStep | None,
        require_verified_fees: bool = True,
        history_limit: int = 1600,
    ) -> None:
        if strategy_step is None:
            raise ValueError("strategy_step is required; refusing a no-op backtest")
        if history_limit < 100:
            raise ValueError("history_limit must be at least 100")
        if not fees:
            raise ValueError("at least one fee schedule is required")
        if require_verified_fees:
            unverified = sorted(product for product, spec in fees.items() if not spec.verified)
            if unverified:
                raise ValueError(f"unverified fee schedules: {', '.join(unverified)}")
        self.fees = dict(fees)
        self.strategy_step = strategy_step
        self.history_limit = history_limit

    def run(self, events: Iterable[ReplayBar]) -> BacktestResult:
        m5_agg = SequentialAggregator(5, "300s")
        m15_agg = SequentialAggregator(15, "900s")
        m5_history: dict[str, list[Bar]] = defaultdict(list)
        m15_history: dict[str, list[Bar]] = defaultdict(list)
        positions: dict[str, Position] = {}
        pending: dict[str, SignalIntent] = {}
        result = BacktestResult()

        ordered = sorted(events, key=lambda item: (item.bar.eob, item.bar.symbol))
        last_eob: dict[str, object] = {}
        for event in ordered:
            bar = event.bar
            product = _product_from_symbol(bar.symbol)
            if product not in self.fees:
                raise ValueError(f"missing fee schedule for {product}")
            fees = self.fees[product]
            previous = last_eob.get(bar.symbol)
            if previous is not None and bar.eob <= previous:  # type: ignore[operator]
                raise ValueError(f"non-increasing bars for {bar.symbol}")
            last_eob[bar.symbol] = bar.eob

            position = positions.get(bar.symbol)
            if position is not None:
                matched = match_exit_in_bar(position, bar, fees, trading_date=event.trading_date)
                if matched is not None:
                    fill, trade = matched
                    result.fills.append(fill)
                    result.trades.append(trade)
                    del positions[bar.symbol]

            intent = pending.pop(bar.symbol, None)
            if intent is not None and bar.symbol not in positions:
                raw_price = intent.entry_price if intent.entry_price is not None else bar.open
                position, fill = open_position(
                    symbol=bar.symbol,
                    side=intent.side,
                    volume=intent.volume,
                    raw_price=raw_price,
                    stop_price=intent.stop_price,
                    target_price=intent.target_price,
                    opened_at=bar.bob,
                    trading_date=event.trading_date,
                    path=intent.path,
                    fees=fees,
                )
                positions[bar.symbol] = position
                result.fills.append(fill)
                matched = match_exit_in_bar(position, bar, fees, trading_date=event.trading_date)
                if matched is not None:
                    exit_fill, trade = matched
                    result.fills.append(exit_fill)
                    result.trades.append(trade)
                    del positions[bar.symbol]

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
                    position=positions.get(bar.symbol),
                )
            )
            if not isinstance(output, StepOutput):
                raise TypeError("strategy_step must return StepOutput")
            result.facts.append(FactRecord(bar.symbol, m15_closed.eob, dict(output.facts)))
            if output.blocked_at:
                result.blocked_at.append(output.blocked_at)
            if output.signal is not None:
                if output.signal.symbol != bar.symbol:
                    raise ValueError("strategy signal symbol does not match event")
                if bar.symbol in positions or bar.symbol in pending:
                    result.blocked_at.append("POSITION_EXISTS")
                else:
                    pending[bar.symbol] = output.signal

        result.open_positions = positions
        return result


def _product_from_symbol(symbol: str) -> str:
    tail = symbol.split(".")[-1]
    letters = "".join(char for char in tail if char.isalpha())
    if not letters:
        raise ValueError(f"cannot derive product from symbol {symbol!r}")
    return letters.upper()
