from datetime import date, datetime, timedelta

import pytest

from backtest.execution import apply_position_management, match_exit_in_bar, open_position
from backtest.models import FeeSchedule
from futures_trend_quant.core.models import Bar


def _fees() -> FeeSchedule:
    return FeeSchedule(
        product="AU",
        multiplier=1000,
        price_tick=0.02,
        open_per_lot=10,
        close_per_lot=20,
        close_today_per_lot=30,
        slippage_ticks=1,
        verified=True,
    )


def _bar(low: float, high: float) -> Bar:
    bob = datetime(2026, 10, 20, 9, 1)
    return Bar(
        symbol="SHFE.au2606",
        frequency="60s",
        bob=bob,
        eob=bob + timedelta(minutes=1),
        open=100,
        high=high,
        low=low,
        close=100,
        volume=1,
        amount=1,
        position=1,
        pre_close=100,
    )


def test_long_stop_fills_in_trigger_bar_minus_one_tick_and_charges_close_today() -> None:
    fees = _fees()
    position, entry = open_position(
        symbol="SHFE.au2606",
        side="long",
        volume=1,
        raw_price=100,
        stop_price=99,
        target_price=102,
        opened_at=datetime(2026, 10, 20, 9, 0),
        trading_date=date(2026, 10, 20),
        path="D",
        fees=fees,
    )
    assert entry.price == pytest.approx(100.02)
    fill, trade = match_exit_in_bar(
        position, _bar(98.5, 101), fees, trading_date=date(2026, 10, 20)
    )
    assert fill.price == pytest.approx(98.98)
    assert fill.offset == "close_today"
    assert trade.fees == 40


def test_same_bar_stop_and_target_uses_adverse_stop() -> None:
    fees = _fees()
    position, _ = open_position(
        symbol="SHFE.au2606",
        side="short",
        volume=1,
        raw_price=100,
        stop_price=101,
        target_price=98,
        opened_at=datetime(2026, 10, 19, 21, 0),
        trading_date=date(2026, 10, 20),
        path="A",
        fees=fees,
    )
    fill, _ = match_exit_in_bar(position, _bar(97, 102), fees, trading_date=date(2026, 10, 21))
    assert fill.reason == "STOP"
    assert fill.price == pytest.approx(101.02)
    assert fill.offset == "close"


def test_be_at_1r_arms_and_same_bar_retrace_uses_break_even_stop() -> None:
    fees = _fees()
    position, _ = open_position(
        symbol="SHFE.au2606",
        side="long",
        volume=1,
        raw_price=100,
        stop_price=99,
        target_price=102,
        opened_at=datetime(2026, 10, 20, 9, 0),
        trading_date=date(2026, 10, 20),
        path="D",
        fees=fees,
        pm_mode="be_at_1r",
    )
    bar = _bar(99.5, 101.1)
    assert match_exit_in_bar(position, bar, fees, trading_date=date(2026, 10, 20)) is None
    assert apply_position_management(position, bar) is True
    fill, trade = match_exit_in_bar(position, bar, fees, trading_date=date(2026, 10, 20))
    assert position.break_even_armed is True
    assert position.stop_price == position.entry_price
    assert fill.reason == "BREAK_EVEN_STOP"
    assert trade.reason == "BREAK_EVEN_STOP"


def test_original_stop_wins_before_break_even_can_arm() -> None:
    fees = _fees()
    position, _ = open_position(
        symbol="SHFE.au2606",
        side="long",
        volume=1,
        raw_price=100,
        stop_price=99,
        target_price=102,
        opened_at=datetime(2026, 10, 20, 9, 0),
        trading_date=date(2026, 10, 20),
        path="D",
        fees=fees,
        pm_mode="be_at_1r",
    )
    fill, _ = match_exit_in_bar(
        position, _bar(98.5, 101.1), fees, trading_date=date(2026, 10, 20)
    )
    assert fill.reason == "STOP"
    assert position.break_even_armed is False
