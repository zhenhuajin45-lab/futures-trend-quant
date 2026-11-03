from __future__ import annotations

from datetime import date

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
) -> tuple[Position, Fill]:
    slip = fees.slippage_ticks * fees.price_tick
    fill_price = raw_price + slip if side == "long" else raw_price - slip
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
