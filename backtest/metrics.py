from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from math import sqrt
from statistics import mean, pstdev
from typing import Iterable, Mapping, Sequence

from futures_trend_quant.core.models import Bar

from .models import Trade


def trade_metrics(trades: Iterable[Trade]) -> dict[str, float | int]:
    rows = list(trades)
    if not rows:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "sharpe_per_trade": 0.0,
        }
    pnls = [trade.net_pnl for trade in rows]
    gains = sum(value for value in pnls if value > 0)
    losses = -sum(value for value in pnls if value < 0)
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    sigma = pstdev(pnls)
    return {
        "trade_count": len(rows),
        "win_rate": sum(value > 0 for value in pnls) / len(rows),
        "net_pnl": sum(pnls),
        "profit_factor": gains / losses if losses else (float("inf") if gains else 0.0),
        "max_drawdown": drawdown,
        "sharpe_per_trade": mean(pnls) / sigma * sqrt(len(pnls)) if sigma else 0.0,
    }


def grouped_trade_metrics(trades: Iterable[Trade]) -> dict[str, dict[str, dict[str, float | int]]]:
    groups: dict[str, dict[str, list[Trade]]] = {
        "symbol": defaultdict(list),
        "path": defaultdict(list),
        "month": defaultdict(list),
    }
    for trade in trades:
        groups["symbol"][trade.symbol].append(trade)
        groups["path"][trade.path].append(trade)
        groups["month"][trade.closed_at.strftime("%Y-%m")].append(trade)
    return {
        dimension: {key: trade_metrics(values) for key, values in buckets.items()}
        for dimension, buckets in groups.items()
    }


def blocked_distribution(blocked_at: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(blocked_at).items()))


def future_reference_direction(
    bars: Sequence[Bar], atr: Sequence[float | None], *, horizon: int = 48, threshold_atr: float = 3.0
) -> list[int | None]:
    """Create a no-lookahead-evaluation label using future range excursions.

    This label is only for post-run trend scoring.  It must never be exposed to
    the strategy callback.  ``1``/``-1`` means the future close first reaches a
    threshold in that direction; ``0`` means neither; ties are unresolved.
    """

    if len(bars) != len(atr):
        raise ValueError("bars and atr must have equal length")
    labels: list[int | None] = []
    for index, bar in enumerate(bars):
        value = atr[index]
        if value is None or value <= 0 or index + horizon >= len(bars):
            labels.append(None)
            continue
        up_level = bar.close + threshold_atr * value
        down_level = bar.close - threshold_atr * value
        label = 0
        for future in bars[index + 1 : index + horizon + 1]:
            up_hit = future.high >= up_level
            down_hit = future.low <= down_level
            if up_hit and down_hit:
                label = 0
                break
            if up_hit:
                label = 1
                break
            if down_hit:
                label = -1
                break
        labels.append(label)
    return labels


def future_48bar_3atr_zigzag(bars: Sequence[Bar], atr: Sequence[float | None]) -> list[int | None]:
    """Pre-registered post-run reference label (48 bars, 3 ATR reversal distance)."""

    return future_reference_direction(bars, atr, horizon=48, threshold_atr=3.0)


def trend_layer_metrics(predicted: Sequence[int], reference: Sequence[int | None]) -> dict[str, float | int]:
    if len(predicted) != len(reference):
        raise ValueError("predicted and reference must have equal length")
    pairs = [(pred, ref) for pred, ref in zip(predicted, reference) if ref is not None]
    directional = [(pred, ref) for pred, ref in pairs if ref != 0]
    predicted_directional = [(pred, ref) for pred, ref in pairs if pred != 0]
    flips = sum(predicted[index] != predicted[index - 1] for index in range(1, len(predicted)))
    return {
        "evaluated_bars": len(pairs),
        "directional_recall": (
            sum(pred == ref for pred, ref in directional) / len(directional) if directional else 0.0
        ),
        "directional_precision": (
            sum(pred == ref for pred, ref in predicted_directional) / len(predicted_directional)
            if predicted_directional
            else 0.0
        ),
        "coverage": len(predicted_directional) / len(pairs) if pairs else 0.0,
        "flip_rate": flips / max(len(predicted) - 1, 1),
    }
