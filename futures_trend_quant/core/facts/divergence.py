from __future__ import annotations

from typing import Literal

from ..models import Divergence, MacdSegment, Pivot
from .indicators import IndicatorSet


def _segment_for(index: int, segments: tuple[MacdSegment, ...]) -> MacdSegment | None:
    return next((segment for segment in segments if segment.start <= index <= segment.end), None)


def _valid_number(value: float | None) -> bool:
    return value is not None


def _a47_channels(
    kind: Literal["top", "bottom"],
    previous: Pivot,
    current: Pivot,
    indicators: IndicatorSet,
) -> tuple[str, ...]:
    previous_segment = _segment_for(previous.index, indicators.macd_segments)
    current_segment = _segment_for(current.index, indicators.macd_segments)
    required_sign = "positive" if kind == "top" else "negative"
    if (
        previous_segment is None
        or current_segment is None
        or previous_segment is current_segment
        or previous_segment.sign != required_sign
        or current_segment.sign != required_sign
        or not previous_segment.complete
    ):
        return ()

    pdif, pdea = indicators.dif[previous.index], indicators.dea[previous.index]
    cdif, cdea = indicators.dif[current.index], indicators.dea[current.index]
    if not all(_valid_number(value) for value in (pdif, pdea, cdif, cdea)):
        return ()

    channels: list[str] = []
    if kind == "top":
        if float(cdif) < float(pdif) and float(cdea) < float(pdea):
            channels.append("lines")
    elif float(cdif) > float(pdif) and float(cdea) > float(pdea):
        channels.append("lines")
    if current_segment.peak_abs < previous_segment.peak_abs:
        channels.append("peak")
    if current_segment.area_abs < previous_segment.area_abs:
        channels.append("area")
    return tuple(channels)


def _b_hist_channels(
    kind: Literal["top", "bottom"],
    previous: Pivot,
    current: Pivot,
    indicators: IndicatorSet,
    dif_atr_threshold: float,
    hist_atr_threshold: float,
    hist_ratio_threshold: float,
) -> tuple[str, ...]:
    pdif, cdif = indicators.dif[previous.index], indicators.dif[current.index]
    phist, chist = indicators.hist[previous.index], indicators.hist[current.index]
    if not all(_valid_number(value) for value in (pdif, cdif, phist, chist)):
        return ()
    # B_hist is meaningful only when the leading extreme is on the expected
    # MACD side. Without this guard, a price high below the zero axis (or a low
    # above it) is incorrectly promoted to a divergence.
    if kind == "top" and (float(pdif) <= 0 or float(phist) <= 0):
        return ()
    if kind == "bottom" and (float(pdif) >= 0 or float(phist) >= 0):
        return ()
    atr_value = current.atr
    channels: list[str] = []

    dif_reduction = float(pdif) - float(cdif) if kind == "top" else float(cdif) - float(pdif)
    if dif_reduction >= dif_atr_threshold * atr_value:
        channels.append("dif")

    required_hist_reduction = max(hist_atr_threshold * atr_value, hist_ratio_threshold * abs(float(phist)))
    if abs(float(phist)) - abs(float(chist)) >= required_hist_reduction:
        channels.append("hist")
    return tuple(channels)


def detect_divergence(
    previous: Pivot,
    current: Pivot,
    indicators: IndicatorSet,
    *,
    mode: Literal["A47", "B_hist"],
    dif_atr_threshold: float = 0.05,
    hist_atr_threshold: float = 0.05,
    hist_ratio_threshold: float = 0.12,
) -> Divergence | None:
    if previous.kind != current.kind:
        raise ValueError("divergence pivots must have the same kind")
    kind: Literal["top", "bottom"] = "top" if current.kind == "high" else "bottom"
    if mode == "A47":
        channels = _a47_channels(kind, previous, current, indicators)
        if len(channels) < 2:
            return None
        quality = 3 if len(channels) == 3 else 2
    elif mode == "B_hist":
        channels = _b_hist_channels(
            kind,
            previous,
            current,
            indicators,
            dif_atr_threshold,
            hist_atr_threshold,
            hist_ratio_threshold,
        )
        if not channels:
            return None
        quality = 3 if len(channels) >= 2 else 2
    else:
        raise ValueError(f"unsupported divergence mode: {mode}")

    return Divergence(
        kind=kind,
        mode=mode,
        previous_index=previous.index,
        current_index=current.index,
        defined_at=max(previous.defined_at, current.defined_at),
        quality=quality,
        channels=channels,
        extreme_price=current.price,
    )
