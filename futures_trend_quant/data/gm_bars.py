from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from ..core.models import Bar


REQUIRED_BAR_FIELDS = ("symbol", "frequency", "bob", "eob", "open", "high", "low", "close")


def _datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    return parsed


def bar_from_mapping(raw: Mapping[str, Any]) -> Bar:
    missing = [field for field in REQUIRED_BAR_FIELDS if field not in raw]
    if missing:
        raise ValueError(f"missing bar fields: {missing}")
    return Bar(
        symbol=str(raw["symbol"]),
        frequency=str(raw["frequency"]),
        bob=_datetime(raw["bob"], "bob"),
        eob=_datetime(raw["eob"], "eob"),
        open=float(raw["open"]),
        high=float(raw["high"]),
        low=float(raw["low"]),
        close=float(raw["close"]),
        volume=float(raw.get("volume", 0) or 0),
        amount=float(raw.get("amount", 0) or 0),
        position=float(raw.get("position", 0) or 0),
        pre_close=None if raw.get("pre_close") in (None, "") else float(raw["pre_close"]),
    )


def bars_from_records(records: Iterable[Mapping[str, Any]]) -> list[Bar]:
    bars = [bar_from_mapping(record) for record in records]
    for previous, current in zip(bars, bars[1:]):
        if current.symbol != previous.symbol or current.frequency != previous.frequency:
            raise ValueError("bar series mixes symbols or frequencies")
        if current.eob <= previous.eob:
            raise ValueError("bar series is not strictly ordered")
    return bars
