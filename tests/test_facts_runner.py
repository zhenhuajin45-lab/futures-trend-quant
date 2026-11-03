from datetime import date

import pytest

from backtest.facts_runner import FactsOnlyRunner
from backtest.runner import StepOutput
from backtest.smoke import synthetic_events


def test_facts_runner_never_requires_fees_or_emits_performance_state() -> None:
    calls = []

    def step(event):
        calls.append(event)
        return StepOutput(facts={"eob": event.m15_bar.eob.isoformat()}, blocked_at="NO_PATH")

    result = FactsOnlyRunner(strategy_step=step).run(synthetic_events(31))
    assert len(calls) == 2
    assert len(result.facts) == 2
    assert len(result.m15_bars) == 2
    assert result.blocked_at == ["NO_PATH", "NO_PATH"]
    assert all(item == date(2026, 1, 5) for item in result.trading_dates)


def test_facts_runner_rejects_any_executable_intent() -> None:
    from backtest.models import SignalIntent

    def step(event):
        return StepOutput(
            signal=SignalIntent(
                symbol=event.symbol,
                side="long",
                volume=1,
                stop_price=99,
                target_price=101,
                path="D",
                created_at=event.m15_bar.eob,
            )
        )

    with pytest.raises(ValueError, match="must not emit"):
        FactsOnlyRunner(strategy_step=step).run(synthetic_events(16))
