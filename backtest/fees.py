from __future__ import annotations

import json
from pathlib import Path

from .models import FeeSchedule


class UnverifiedFeeConfigError(ValueError):
    pass


def load_fee_schedules(path: str | Path, *, require_verified: bool = True) -> dict[str, FeeSchedule]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    schedules: dict[str, FeeSchedule] = {}
    for product, item in raw.get("products", {}).items():
        fee_keys = (
            "open_rate",
            "close_rate",
            "close_today_rate",
            "open_per_lot",
            "close_per_lot",
            "close_today_per_lot",
        )
        if any(item.get(key) is None for key in fee_keys):
            raise UnverifiedFeeConfigError(f"{product} has unresolved fee fields")
        schedule = FeeSchedule(
            product=product,
            multiplier=float(item["multiplier"]),
            price_tick=float(item["price_tick"]),
            open_rate=float(item["open_rate"]),
            close_rate=float(item["close_rate"]),
            close_today_rate=float(item["close_today_rate"]),
            open_per_lot=float(item["open_per_lot"]),
            close_per_lot=float(item["close_per_lot"]),
            close_today_per_lot=float(item["close_today_per_lot"]),
            slippage_ticks=int(item["slippage_ticks"]),
            verified=bool(item.get("verified", False)),
        )
        if require_verified and not schedule.verified:
            raise UnverifiedFeeConfigError(f"{product} fee schedule is not verified")
        schedules[product] = schedule
    if not schedules:
        raise UnverifiedFeeConfigError("no product fee schedules found")
    return schedules
