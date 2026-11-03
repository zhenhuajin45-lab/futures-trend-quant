from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isclose
from typing import Iterable, Mapping


DEFAULT_FACT_KEYS = (
    "close",
    "ema34",
    "atr14",
    "slope_state",
    "h1_absolute",
    "top_div",
    "bottom_div",
    "trend_decision",
)


@dataclass(frozen=True)
class FactMismatch:
    symbol: str
    eob: object
    key: str
    offline: object
    gm: object
    reason: str


def compare_m15_facts(
    offline_rows: Iterable[Mapping[str, object]],
    gm_rows: Iterable[Mapping[str, object]],
    *,
    keys: tuple[str, ...] = DEFAULT_FACT_KEYS,
    tolerance: float = 1e-6,
) -> list[FactMismatch]:
    """Compare normalized offline and GM M15_FACTS rows one-for-one."""

    def index(rows):
        output = {}
        for row in rows:
            identity = (row.get("symbol"), row.get("eob"))
            if None in identity:
                raise ValueError("fact rows require symbol and eob")
            if identity in output:
                raise ValueError(f"duplicate fact row {identity}")
            output[identity] = row
        return output

    offline = index(offline_rows)
    gm = index(gm_rows)
    mismatches: list[FactMismatch] = []
    for identity in sorted(set(offline) | set(gm), key=lambda item: (str(item[0]), str(item[1]))):
        left = offline.get(identity)
        right = gm.get(identity)
        if left is None or right is None:
            mismatches.append(
                FactMismatch(
                    symbol=str(identity[0]),
                    eob=identity[1],
                    key="*",
                    offline=None if left is None else "present",
                    gm=None if right is None else "present",
                    reason="missing_row",
                )
            )
            continue
        for key in keys:
            left_value = left.get(key)
            right_value = right.get(key)
            if isinstance(left_value, (float, int)) and isinstance(right_value, (float, int)):
                equal = isclose(float(left_value), float(right_value), rel_tol=0.0, abs_tol=tolerance)
            else:
                equal = left_value == right_value
            if not equal:
                mismatches.append(
                    FactMismatch(str(identity[0]), identity[1], key, left_value, right_value, "value")
                )
    return mismatches
