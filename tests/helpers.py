from __future__ import annotations

from datetime import datetime, timedelta

from futures_trend_quant.core.models import Bar


def make_bars(count: int, *, start: float = 100.0, step: float = 0.2, symbol: str = "SHFE.AU2606") -> list[Bar]:
    begin = datetime(2026, 1, 5, 9, 0)
    bars: list[Bar] = []
    previous_close = start
    for index in range(count):
        close = start + index * step + ((index % 5) - 2) * 0.03
        open_price = previous_close
        high = max(open_price, close) + 0.4
        low = min(open_price, close) - 0.4
        bob = begin + timedelta(minutes=15 * index)
        bars.append(
            Bar(
                symbol=symbol,
                frequency="900s",
                bob=bob,
                eob=bob + timedelta(minutes=15),
                open=open_price,
                high=high,
                low=low,
                close=close,
                pre_close=previous_close,
            )
        )
        previous_close = close
    return bars
