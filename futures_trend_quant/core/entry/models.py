from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


Direction = Literal["bull", "bear"]
PathName = Literal["A", "B", "C", "D"]


@dataclass(frozen=True)
class EvidenceEvent:
    """A closed-bar fact and the bar on which it first became knowable."""

    kind: str
    direction: Direction
    bar_id: str
    sequence: int
    eob: datetime | None = None


@dataclass(frozen=True)
class EntryEvidence:
    """Legacy Path-D result kept stable for the GM adapter."""

    accepted: bool
    direction: Direction
    evidence_id: str
    reason: str
    m5_event: str | None
    m1_reverse: bool


@dataclass(frozen=True)
class EvidenceSnapshot:
    """All entry facts calculated from closed M5/M1 bars."""

    direction: Direction
    m5_divergence: EvidenceEvent | None = None
    m5_flatten: EvidenceEvent | None = None
    m5_pattern: EvidenceEvent | None = None
    m5_pullback: EvidenceEvent | None = None
    m5_consolidation: EvidenceEvent | None = None
    m5_breakout: EvidenceEvent | None = None
    m5_ema_cross: EvidenceEvent | None = None
    m5_engulfing: EvidenceEvent | None = None
    m5_reverse_divergence: bool = False
    m1_confirmation: EvidenceEvent | None = None
    m1_strong_reverse: bool = False
    m5_last_sequence: int | None = None


@dataclass(frozen=True)
class EntryContext:
    direction: Direction
    trend_confirmed: bool
    h1_state: str | None  # Absolute finalized entry H1, absent until sufficiently warmed.
    m15_slope_state: str
    m15_reverse_divergence: bool
    gate_open: bool
    same_direction_positions: int = 0
    m15_index: int = 0
    current_price: float = 0.0
    atr14: float = 0.0
    last_same_direction_fill_index: int | None = None
    last_same_direction_fill_price: float | None = None
    used_evidence_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class EntrySettings:
    path_a_enabled: bool = True
    path_b_enabled: bool = True
    path_c_enabled: bool = True
    path_d_enabled: bool = True
    path_b_h1_exception: bool = False
    m1_confirmation_enabled: bool = False
    cooldown_enabled: bool = True
    cooldown_bars: int = 4
    path_d_add_distance_atr: float = 1.5
    path_d_max_positions: int = 2
    pm_mode: Literal["fixed", "be_at_1r"] = "fixed"

    def __post_init__(self) -> None:
        if self.cooldown_bars < 0:
            raise ValueError("cooldown_bars must be non-negative")
        if self.path_d_add_distance_atr < 0:
            raise ValueError("path_d_add_distance_atr must be non-negative")
        if self.path_d_max_positions < 1:
            raise ValueError("path_d_max_positions must be positive")


@dataclass(frozen=True)
class PathCandidate:
    path: PathName
    direction: Direction
    trigger_bar_id: str
    trigger_sequence: int
    trigger_eob: datetime | None = None
    process_id: str | None = None

    @property
    def evidence_id(self) -> str:
        if self.process_id:return f"{self.path}|{self.direction}|{self.process_id}|{self.trigger_bar_id}"
        return f"{self.path}|{self.direction}|{self.trigger_bar_id}"


@dataclass(frozen=True)
class EntryDecision:
    accepted: bool
    direction: Direction
    reason: str
    path: PathName | None = None
    trigger_bar_id: str | None = None
    evidence_id: str | None = None
    candidates: tuple[PathCandidate, ...] = ()
