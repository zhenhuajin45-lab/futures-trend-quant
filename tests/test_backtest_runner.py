from datetime import date, datetime, timedelta

import pytest

from backtest.models import FeeSchedule, SignalIntent
from backtest.runner import OfflineRunner, ReplayBar, StepOutput
from futures_trend_quant.core.models import Bar


def _events(count: int = 16) -> list[ReplayBar]:
    output = []
    start = datetime(2026, 10, 20, 9)
    for index in range(count):
        bob = start + timedelta(minutes=index)
        output.append(
            ReplayBar(
                Bar(
                    symbol="SHFE.au2606",
                    frequency="60s",
                    bob=bob,
                    eob=bob + timedelta(minutes=1),
                    open=100,
                    high=100.5,
                    low=99.5,
                    close=100,
                    volume=1,
                    amount=1,
                    position=1,
                    pre_close=100,
                ),
                date(2026, 10, 20),
            )
        )
    return output


def _fees(verified: bool = True) -> dict[str, FeeSchedule]:
    return {
        "AU": FeeSchedule(
            product="AU", multiplier=1000, price_tick=0.02, slippage_ticks=1, verified=verified
        )
    }


def test_runner_calls_step_only_on_closed_m15_and_enters_next_m1() -> None:
    calls = []

    def step(event):
        calls.append(event)
        return StepOutput(
            signal=SignalIntent(
                symbol=event.symbol,
                side="long",
                volume=1,
                stop_price=98,
                target_price=102,
                path="D",
                created_at=event.m15_bar.eob,
            ),
            facts={"close": event.m15_bar.close},
        )

    result = OfflineRunner(fees=_fees(), strategy_step=step).run(_events())
    assert len(calls) == 1
    assert len(result.facts) == 1
    assert len(result.fills) == 1
    assert result.fills[0].timestamp == datetime(2026, 10, 20, 9, 15)


def test_runner_fails_closed_for_unverified_fees_or_missing_step() -> None:
    with pytest.raises(ValueError, match="unverified"):
        OfflineRunner(fees=_fees(False), strategy_step=lambda event: StepOutput())
    with pytest.raises(ValueError, match="strategy_step"):
        OfflineRunner(fees=_fees(), strategy_step=None)
