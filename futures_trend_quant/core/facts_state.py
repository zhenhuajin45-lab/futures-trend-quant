from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .facts.divergence import detect_divergence
from .facts.indicators import calculate_indicators
from .facts.swing import find_pivots, previous_swing_pair
from .models import Bar, Divergence, Pivot


@dataclass(frozen=True)
class StablePivot:
    """A confirmed pivot addressed by immutable bar identities, not window offsets."""

    bar_id: str
    defined_at_bar_id: str
    kind: Literal["high", "low"]
    price: float
    atr: float


@dataclass(frozen=True)
class StableDivergence:
    kind: Literal["top", "bottom"]
    mode: Literal["A47", "B_hist"]
    previous_bar_id: str
    current_bar_id: str
    defined_at_bar_id: str
    quality: int
    channels: tuple[str, ...]
    extreme_price: float


@dataclass
class FactsState:
    """Append-only confirmed facts used across rolling calculation windows."""

    pivots: list[StablePivot] = field(default_factory=list)
    divergences: list[StableDivergence] = field(default_factory=list)
    observed_bar_ids: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "pivots": [asdict(item) for item in self.pivots],
            "divergences": [asdict(item) for item in self.divergences],
            "observed_bar_ids": sorted(self.observed_bar_ids),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FactsState":
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported facts-state schema")
        return cls(
            pivots=[StablePivot(**item) for item in raw.get("pivots", [])],
            divergences=[
                StableDivergence(**{**item, "channels": tuple(item["channels"])})
                for item in raw.get("divergences", [])
            ],
            observed_bar_ids=set(raw.get("observed_bar_ids", [])),
        )


@dataclass
class FactsStateBook:
    """Persistence boundary: one independent incremental state per csymbol."""

    by_csymbol: dict[str, FactsState] = field(default_factory=dict)

    def for_csymbol(self, csymbol: str) -> FactsState:
        if not csymbol:
            raise ValueError("csymbol must not be empty")
        return self.by_csymbol.setdefault(csymbol, FactsState())

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, "by_csymbol": {key: value.to_dict() for key, value in self.by_csymbol.items()}}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FactsStateBook":
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported facts-state-book schema")
        return cls({key: FactsState.from_dict(value) for key, value in raw.get("by_csymbol", {}).items()})


def _stable_pivot(pivot: Pivot, bars: list[Bar]) -> StablePivot:
    return StablePivot(
        bar_id=bars[pivot.index].identity,
        defined_at_bar_id=bars[pivot.defined_at].identity,
        kind=pivot.kind,
        price=pivot.price,
        atr=pivot.atr,
    )


def _stable_divergence(divergence: Divergence, bars: list[Bar]) -> StableDivergence:
    return StableDivergence(
        kind=divergence.kind,
        mode=divergence.mode,
        previous_bar_id=bars[divergence.previous_index].identity,
        current_bar_id=bars[divergence.current_index].identity,
        defined_at_bar_id=bars[divergence.defined_at].identity,
        quality=divergence.quality,
        channels=divergence.channels,
        extreme_price=divergence.extreme_price,
    )


def update_facts_state(state: FactsState, bars: list[Bar], rules: dict[str, object]) -> FactsState:
    """Append newly confirmed facts without rewriting facts confirmed in prior windows.

    ``bars`` may be a rolling window. A fact is persisted by the source bar IDs, so
    moving the left edge cannot change an already confirmed pivot or divergence.
    """

    if not bars:
        return state
    indicators = calculate_indicators(bars)
    pivots = find_pivots(
        bars,
        indicators.atr14,
        k=int(rules.get("pivot_k", 2)),
        leg_min_atr=float(rules.get("leg_min_atr", 1.2)),
        mode=str(rules.get("swing_mode", "S_A")),  # type: ignore[arg-type]
    )
    closed_index = len(bars) - 1
    known_pivots = {item.bar_id for item in state.pivots}
    for pivot in pivots:
        if pivot.defined_at > closed_index:
            continue
        stable = _stable_pivot(pivot, bars)
        if stable.bar_id not in known_pivots:
            state.pivots.append(stable)
            known_pivots.add(stable.bar_id)

    known_divergences = {
        (item.kind, item.previous_bar_id, item.current_bar_id, item.mode) for item in state.divergences
    }
    # Only the latest confirmed pivot of each kind may create a new pairing. An
    # older divergence is never resurrected by scanning backwards.
    for kind in ("high", "low"):
        candidates = [item for item in pivots if item.kind == kind and item.defined_at <= closed_index]
        if not candidates:
            continue
        current = candidates[-1]
        pair = previous_swing_pair(
            pivots,
            bars,
            current_index=current.index,
            kind=kind,
            min_span_bars=int(rules.get("min_span_bars", 5)),
            max_span_bars=int(rules.get("max_span_bars", 120)),
            new_extreme_min_atr=float(rules.get("new_extreme_min_atr", 0.1)),
        )
        if pair is None:
            continue
        divergence = detect_divergence(
            pair[0], pair[1], indicators, mode=str(rules.get("div_mode", "B_hist"))  # type: ignore[arg-type]
        )
        if divergence is None:
            continue
        stable = _stable_divergence(divergence, bars)
        key = (stable.kind, stable.previous_bar_id, stable.current_bar_id, stable.mode)
        if key not in known_divergences:
            state.divergences.append(stable)
            known_divergences.add(key)
    state.observed_bar_ids.update(bar.identity for bar in bars)
    return state


def latest_valid_divergence(
    state: FactsState, bars: list[Bar], kind: Literal["top", "bottom"]
) -> StableDivergence | None:
    """Return the newest same-kind divergence while its extreme remains intact."""

    candidates = [item for item in state.divergences if item.kind == kind]
    if not candidates:
        return None
    latest = candidates[-1]
    index_by_id = {bar.identity: index for index, bar in enumerate(bars)}
    current_index = index_by_id.get(latest.current_bar_id)
    # If the defining pivot has already fallen off the rolling window, every
    # remaining bar is later than it and must still be checked for invalidation.
    subsequent = bars if current_index is None else bars[current_index + 1 :]
    if kind == "top" and any(bar.high > latest.extreme_price for bar in subsequent):
        return None
    if kind == "bottom" and any(bar.low < latest.extreme_price for bar in subsequent):
        return None
    return latest
