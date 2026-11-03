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


def future_first_touch_direction(
    bars: Sequence[Bar], atr: Sequence[float | None], *, horizon: int = 48, threshold_atr: float = 3.0
) -> list[int | None]:
    """Create a post-run future-first-touch label.

    This label is only for post-run trend scoring.  It must never be exposed to
    the strategy callback.  ``1``/``-1`` means a future bar first reaches a
    threshold in that direction with high/low; ``0`` means neither or a
    same-bar double touch.  This is not a ZigZag or an independent turn event.
    """

    if len(bars) != len(atr):
        raise ValueError("bars and atr must have equal length")
    if bars and len({bar.symbol for bar in bars}) != 1:
        raise ValueError("future-first-touch reference must not cross symbols")
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


def future_48bar_3atr_first_touch(
    bars: Sequence[Bar], atr: Sequence[float | None]
) -> list[int | None]:
    """Frozen FUTURE_FIRST_TOUCH_V1 label (48 bars, 3 ATR)."""

    return future_first_touch_direction(bars, atr, horizon=48, threshold_atr=3.0)


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


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    if not 0 <= percentile <= 1:
        raise ValueError("percentile must be in [0, 1]")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _direction(value: int | str | None) -> int | None:
    if value in (1, "bull"):
        return 1
    if value in (-1, "bear"):
        return -1
    if value in (0, "none", "neutral", "range"):
        return 0
    if value is None:
        return None
    raise ValueError(f"unsupported direction value: {value!r}")


def score_legacy_first_touch_metrics(
    bars: Sequence[Bar],
    atr: Sequence[float | None],
    predicted: Sequence[int | str | None],
    reference: Sequence[int | str | None],
    trading_dates: Sequence[object],
    symbols: Sequence[str] | None = None,
) -> dict[str, float | int | bool]:
    """Reproduce the legacy four-metric report.

    ``reference`` is supplied explicitly so the scorer can be tested without
    future leakage.  The turn-delay fields in this function treat overlapping
    first-touch label changes as events and are retained only for historical
    report reproducibility.  P0-B v2 uses independent directional-change
    events for turn delay.
    """

    symbol_values = list(symbols) if symbols is not None else [bar.symbol for bar in bars]
    lengths = {
        len(bars), len(atr), len(predicted), len(reference), len(trading_dates), len(symbol_values)
    }
    if len(lengths) != 1:
        raise ValueError("trend scoring inputs must have equal length")
    predicted_values = [_direction(value) for value in predicted]
    reference_values = [_direction(value) for value in reference]

    directional_indices = [index for index, value in enumerate(reference_values) if value in (-1, 1)]
    direction_correct = sum(
        predicted_values[index] == reference_values[index] for index in directional_indices
    )
    direction_agreement = direction_correct / len(directional_indices) if directional_indices else 0.0

    segment_coverages: list[float] = []
    segment_bars = 0
    segment_correct = 0
    index = 0
    while index < len(reference_values):
        direction = reference_values[index]
        if direction not in (-1, 1):
            index += 1
            continue
        end = index + 1
        while (
            end < len(reference_values)
            and symbol_values[end] == symbol_values[index]
            and reference_values[end] == direction
        ):
            end += 1
        start_atr = atr[index]
        if start_atr is not None and start_atr > 0:
            if direction == 1:
                excursion = max(bar.high for bar in bars[index:end]) - bars[index].close
            else:
                excursion = bars[index].close - min(bar.low for bar in bars[index:end])
            if excursion >= 2 * start_atr:
                correct = sum(predicted_values[item] == direction for item in range(index, end))
                count = end - index
                segment_coverages.append(correct / count)
                segment_bars += count
                segment_correct += correct
        index = end
    segment_coverage = segment_correct / segment_bars if segment_bars else 0.0

    range_switches_by_day: dict[object, int] = {}
    last_range_direction: dict[object, int] = {}
    for index, reference_value in enumerate(reference_values):
        if reference_value != 0:
            continue
        key = (symbol_values[index], trading_dates[index])
        range_switches_by_day.setdefault(key, 0)
        direction = predicted_values[index]
        if direction not in (-1, 1):
            continue
        previous = last_range_direction.get(key)
        if previous is not None and previous != direction:
            range_switches_by_day[key] += 1
        last_range_direction[key] = direction
    range_values = [float(value) for value in range_switches_by_day.values()]
    range_average = mean(range_values) if range_values else 0.0

    turn_delays: list[float] = []
    unresolved = 0
    prior_directions: dict[str, int] = {}
    for index, current in enumerate(reference_values):
        if current not in (-1, 1):
            continue
        symbol = symbol_values[index]
        prior_direction = prior_directions.get(symbol)
        if prior_direction is not None and current != prior_direction:
            resolved_delay: int | None = None
            last_candidate = min(len(predicted_values) - 2, index + 48)
            for candidate in range(index, last_candidate + 1):
                if symbol_values[candidate] != symbol or symbol_values[candidate + 1] != symbol:
                    break
                if predicted_values[candidate] == current and predicted_values[candidate + 1] == current:
                    resolved_delay = candidate - index
                    break
            if resolved_delay is None:
                unresolved += 1
                turn_delays.append(49.0)
            else:
                turn_delays.append(float(resolved_delay))
        prior_directions[symbol] = current

    return {
        "evaluated_bars": sum(value is not None for value in reference_values),
        "directional_reference_bars": len(directional_indices),
        "direction_agreement": direction_agreement,
        "direction_agreement_passed": direction_agreement >= 0.60,
        "eligible_trend_segments": len(segment_coverages),
        "trend_segment_bars": segment_bars,
        "trend_segment_coverage": segment_coverage,
        "trend_segment_coverage_p25": _percentile(segment_coverages, 0.25),
        "trend_segment_coverage_median": _percentile(segment_coverages, 0.50),
        "trend_segment_coverage_p75": _percentile(segment_coverages, 0.75),
        "trend_segment_coverage_passed": segment_coverage >= 0.70,
        "range_product_days": len(range_switches_by_day),
        "range_switches": int(sum(range_switches_by_day.values())),
        "range_switches_per_product_day": range_average,
        "range_switches_p95": _percentile(range_values, 0.95),
        "range_switches_max": int(max(range_values, default=0.0)),
        "range_switches_passed": range_average <= 1.0,
        "turn_events": len(turn_delays),
        "turn_delay_median": _percentile(turn_delays, 0.50),
        "turn_delay_p75": _percentile(turn_delays, 0.75),
        "turn_delay_p90": _percentile(turn_delays, 0.90),
        "unresolved_turn_rate": unresolved / len(turn_delays) if turn_delays else 0.0,
        "turn_delay_passed": bool(turn_delays) and _percentile(turn_delays, 0.50) <= 4.0,
    }


def legacy_first_touch_metrics(
    bars: Sequence[Bar],
    atr: Sequence[float | None],
    predicted: Sequence[int | str | None],
    trading_dates: Sequence[object],
    symbols: Sequence[str] | None = None,
) -> dict[str, float | int | bool]:
    """Build the legacy first-touch report without renaming it ZigZag."""

    symbol_values = list(symbols) if symbols is not None else [bar.symbol for bar in bars]
    if len(symbol_values) != len(bars):
        raise ValueError("symbols and bars must have equal length")
    indices_by_symbol: dict[str, list[int]] = defaultdict(list)
    for index, symbol in enumerate(symbol_values):
        indices_by_symbol[symbol].append(index)
    reference: list[int | None] = [None] * len(bars)
    for indices in indices_by_symbol.values():
        local = future_48bar_3atr_first_touch(
            [bars[index] for index in indices],
            [atr[index] for index in indices],
        )
        for index, label in zip(indices, local):
            reference[index] = label
    return score_legacy_first_touch_metrics(
        bars, atr, predicted, reference, trading_dates, symbols=symbols
    )
