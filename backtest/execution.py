from __future__ import annotations

from datetime import date
import math

from futures_trend_quant.core.models import Bar

from .models import FeeSchedule, Fill, Position, Trade


def _exit_offset(position: Position, trading_date: date) -> str:
    return "close_today" if position.trading_date == trading_date else "close"


def open_position(
    *,
    symbol: str,
    side: str,
    volume: int,
    raw_price: float,
    stop_price: float,
    target_price: float,
    opened_at,
    trading_date: date,
    path: str,
    fees: FeeSchedule,
    evidence_id: str = "",
    signal_m15_index: int = 0,
) -> tuple[Position, Fill]:
    slip = fees.slippage_ticks * fees.price_tick
    fill_price = raw_price + slip if side == "long" else raw_price - slip
    if side == "long":
        if fill_price <= stop_price:
            raise ValueError("long fill is not above stop")
        target_price = math.ceil(
            (fill_price + 2 * (fill_price - stop_price)) / fees.price_tick - 1e-12
        ) * fees.price_tick
    else:
        if fill_price >= stop_price:
            raise ValueError("short fill is not below stop")
        target_price = math.floor(
            (fill_price - 2 * (stop_price - fill_price)) / fees.price_tick + 1e-12
        ) * fees.price_tick
    fee = fees.fee(fill_price, volume, "open")
    position = Position(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        volume=volume,
        entry_price=fill_price,
        stop_price=stop_price,
        target_price=target_price,
        opened_at=opened_at,
        trading_date=trading_date,
        path=path,
        evidence_id=evidence_id,
        signal_m15_index=signal_m15_index,
        entry_fee=fee,
    )
    return position, Fill(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        offset="open",
        volume=volume,
        price=fill_price,
        timestamp=opened_at,
        fee=fee,
        reason="ENTRY",
    )


def match_exit_in_bar(
    position: Position,
    bar: Bar,
    fees: FeeSchedule,
    *,
    trading_date: date,
) -> tuple[Fill, Trade] | None:
    """Match stops/targets inside the triggering M1 bar.

    If both levels occur in the same bar and tick order is unavailable, the
    adverse (stop) outcome is selected.  A sell is slipped down and a buy is
    slipped up by the configured number of ticks.
    """

    if position.symbol != bar.symbol:
        return None
    if position.side == "long":
        stop_hit = bar.low <= position.stop_price
        target_hit = bar.high >= position.target_price
    else:
        stop_hit = bar.high >= position.stop_price
        target_hit = bar.low <= position.target_price
    if not stop_hit and not target_hit:
        return None

    reason = "STOP" if stop_hit else "TARGET"
    trigger = position.stop_price if stop_hit else position.target_price
    slip = fees.slippage_ticks * fees.price_tick
    exit_price = trigger - slip if position.side == "long" else trigger + slip
    offset = _exit_offset(position, trading_date)
    fee = fees.fee(exit_price, position.volume, offset)  # type: ignore[arg-type]
    direction = 1.0 if position.side == "long" else -1.0
    gross = (exit_price - position.entry_price) * direction * fees.multiplier * position.volume
    total_fees = position.entry_fee + fee
    fill = Fill(
        symbol=position.symbol,
        side=position.side,
        offset=offset,  # type: ignore[arg-type]
        volume=position.volume,
        price=exit_price,
        timestamp=bar.eob,
        fee=fee,
        reason=reason,
    )
    trade = Trade(
        symbol=position.symbol,
        side=position.side,
        volume=position.volume,
        path=position.path,
        opened_at=position.opened_at,
        closed_at=bar.eob,
        entry_price=position.entry_price,
        exit_price=exit_price,
        gross_pnl=gross,
        fees=total_fees,
        net_pnl=gross - total_fees,
        reason=reason,
    )
    return fill, trade


def close_position_at_market(
    position: Position,
    bar: Bar,
    fees: FeeSchedule,
    *,
    trading_date: date,
    reason: str,
) -> tuple[Fill, Trade]:
    """Close at the next M1 open after a bar-close strategy decision."""

    slip = fees.slippage_ticks * fees.price_tick
    exit_price = bar.open - slip if position.side == "long" else bar.open + slip
    offset = _exit_offset(position, trading_date)
    fee = fees.fee(exit_price, position.volume, offset)  # type: ignore[arg-type]
    direction = 1.0 if position.side == "long" else -1.0
    gross = (exit_price - position.entry_price) * direction * fees.multiplier * position.volume
    total_fees = position.entry_fee + fee
    fill = Fill(
        symbol=position.symbol,
        side=position.side,
        offset=offset,  # type: ignore[arg-type]
        volume=position.volume,
        price=exit_price,
        timestamp=bar.bob,
        fee=fee,
        reason=reason,
    )
    trade = Trade(
        symbol=position.symbol,
        side=position.side,
        volume=position.volume,
        path=position.path,
        opened_at=position.opened_at,
        closed_at=bar.bob,
        entry_price=position.entry_price,
        exit_price=exit_price,
        gross_pnl=gross,
        fees=total_fees,
        net_pnl=gross - total_fees,
        reason=reason,
    )
    return fill, trade
