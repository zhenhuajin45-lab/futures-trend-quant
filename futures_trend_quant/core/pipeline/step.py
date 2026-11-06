from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping

from ..entry import (
    AnchorSelection,
    EntryContext,
    EntryDecision,
    EntrySettings,
    GateDecision,
    build_entry_evidence,
    evaluate_entries,
    evaluate_momentum_gate,
    select_anchor,
)
from ..facts.pipeline import MarketAnalysis, analyze_market, trend_facts, aggregate_m15_to_h1, _classify_h1_history
from ..facts.swing import find_pivots
from ..facts_state import FactsStateBook, latest_valid_divergence, update_facts_state
from ..models import Bar, Direction, TrendState
from ..trend.state_machine import TrendDecision, TrendFacts, step_trend
from .permissions import evaluate_permission


def _direction_key(csymbol: str, direction: Direction) -> str:
    return f"{csymbol}|{direction}"


@dataclass
class StrategyRuntimeBook:
    """All deterministic strategy state, keyed by continuous symbol."""

    facts: FactsStateBook = field(default_factory=FactsStateBook)
    trends: dict[str, TrendState] = field(default_factory=dict)
    anchors: dict[str, AnchorSelection] = field(default_factory=dict)
    used_evidence_ids: set[str] = field(default_factory=set)
    m15_indices: dict[str, int] = field(default_factory=dict)
    last_fill_indices: dict[str, int] = field(default_factory=dict)
    last_fill_prices: dict[str, float] = field(default_factory=dict)
    evidence_clocks: dict[str, dict[str, str]] = field(default_factory=dict)
    permission_state: dict[str, dict[str, object]] = field(default_factory=dict)
    anchor_invalidations: dict[str, str] = field(default_factory=dict)

    def trend_for(self, csymbol: str) -> TrendState:
        return self.trends.setdefault(csymbol, TrendState())

    def m15_index_for(self, csymbol: str) -> int:
        return self.m15_indices.get(csymbol, 0)

    def record_fill(
        self,
        *,
        csymbol: str,
        direction: Direction,
        price: float,
        evidence_id: str,
        m15_index: int | None = None,
    ) -> None:
        if price <= 0 or not evidence_id:
            raise ValueError("fill state requires positive price and evidence_id")
        key = _direction_key(csymbol, direction)
        self.last_fill_indices[key] = self.m15_index_for(csymbol) if m15_index is None else m15_index
        self.last_fill_prices[key] = price
        self.used_evidence_ids.add(evidence_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "facts": self.facts.to_dict(),
            "trends": {key: value.to_dict() for key, value in self.trends.items()},
            "anchors": {key: asdict(value) for key, value in self.anchors.items()},
            "used_evidence_ids": sorted(self.used_evidence_ids),
            "m15_indices": self.m15_indices,
            "last_fill_indices": self.last_fill_indices,
            "last_fill_prices": self.last_fill_prices,
            "core_contract": "CLOSED_FACTS_V2",
            "evidence_clocks": self.evidence_clocks,
            "permission_state": self.permission_state,
            "anchor_invalidations": self.anchor_invalidations,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StrategyRuntimeBook":
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported strategy-runtime schema")
        return cls(
            facts=FactsStateBook.from_dict(dict(raw.get("facts", {}))),
            trends={key: TrendState.from_dict(value) for key, value in dict(raw.get("trends", {})).items()},
            anchors={key: AnchorSelection(**value) for key, value in dict(raw.get("anchors", {})).items()},
            used_evidence_ids=set(raw.get("used_evidence_ids", [])),
            m15_indices={key: int(value) for key, value in dict(raw.get("m15_indices", {})).items()},
            last_fill_indices={key: int(value) for key, value in dict(raw.get("last_fill_indices", {})).items()},
            last_fill_prices={key: float(value) for key, value in dict(raw.get("last_fill_prices", {})).items()},
            evidence_clocks=dict(raw.get("evidence_clocks", {})),
            permission_state=dict(raw.get("permission_state", {})),
            anchor_invalidations=dict(raw.get("anchor_invalidations", {})),
        )


@dataclass(frozen=True)
class CoreStepInput:
    csymbol: str
    symbol: str
    m15_bars: tuple[Bar, ...]
    m5_bars: tuple[Bar, ...]
    m1_bars: tuple[Bar, ...]
    rules: Mapping[str, object]
    same_direction_positions: int = 0
    positions_by_direction: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CoreStepResult:
    analysis: MarketAnalysis
    facts: TrendFacts
    trend: TrendDecision
    gate: GateDecision | None
    entry: EntryDecision | None
    anchor: AnchorSelection | None
    facts_row: Mapping[str, object]
    prior_trend: str
    m15_index: int
    permission: Any = None
    evidence_clock: Mapping[str, str] | None = None

    @property
    def trend_changed(self) -> bool:
        return self.trend.confirmed and self.trend.confirmed_state != self.prior_trend


def _entry_settings(rules: Mapping[str, object]) -> EntrySettings:
    return EntrySettings(
        path_a_enabled=bool(rules["path_a_enabled"]),
        path_b_enabled=bool(rules["path_b_enabled"]),
        path_c_enabled=bool(rules["path_c_enabled"]),
        path_d_enabled=bool(rules["path_d_enabled"]),
        path_b_h1_exception=bool(rules["path_b_h1_exception"]),
        m1_confirmation_enabled=bool(rules["m1_confirmation_enabled"]),
        cooldown_enabled=bool(rules["cooldown_enabled"]),
        cooldown_bars=int(rules["cooldown_bars"]),
        path_d_add_distance_atr=float(rules["path_d_cooldown_atr"]),
        path_d_max_positions=int(rules["path_d_max_stack"]),
        pm_mode=str(rules["pm_mode"]),  # type: ignore[arg-type]
    )


def run_core_step(event: CoreStepInput, runtime: StrategyRuntimeBook) -> CoreStepResult:
    """Run the single closed-M15 strategy chain shared by GM and offline replay."""

    if not event.csymbol or not event.symbol:
        raise ValueError("core step requires csymbol and actual symbol")
    if not event.m15_bars or event.m15_bars[-1].symbol != event.symbol:
        raise ValueError("M15 history does not match actual symbol")
    rules = dict(event.rules)
    facts_state = runtime.facts.for_csymbol(event.csymbol)
    update_facts_state(facts_state, list(event.m15_bars), rules)
    analysis = analyze_market(list(event.m15_bars), rules)
    if rules.get('paper_permission_v1'):
        # Separate frequency state: H1 invalidation must survive its own rolling window.
        h1_bars = aggregate_m15_to_h1(list(event.m15_bars))
        h1_state = runtime.facts.for_csymbol(event.csymbol+'|CLOSED_H1')
        update_facts_state(h1_state, h1_bars, rules)
        h1_top_active = latest_valid_divergence(h1_state, h1_bars, 'top') is not None
        h1_bottom_active = latest_valid_divergence(h1_state, h1_bars, 'bottom') is not None
        h1_absolute = _classify_h1_history(h1_bars, rules, (h1_top_active,h1_bottom_active))[0]
        analysis = replace(analysis, h1_absolute=h1_absolute, finalized_h1_absolute=h1_absolute,
                           h1_top_active=h1_top_active, h1_bottom_active=h1_bottom_active)
    prior = runtime.trend_for(event.csymbol)
    prior_name = prior.trend_state
    facts = trend_facts(analysis, prior)

    stable_top = latest_valid_divergence(facts_state, list(event.m15_bars), "top")
    stable_bottom = latest_valid_divergence(facts_state, list(event.m15_bars), "bottom")
    top_active = stable_top is not None
    bottom_active = stable_bottom is not None
    if top_active != facts.top_divergence or bottom_active != facts.bottom_divergence:
        p_macd = facts.p_macd_cross
        if prior_name == "bull":
            p_macd = bool(
                stable_top
                and analysis.macd_cross == "down"
                and stable_top.defined_at_bar_id != analysis.bar.identity
            )
        elif prior_name == "bear":
            p_macd = bool(
                stable_bottom
                and analysis.macd_cross == "up"
                and stable_bottom.defined_at_bar_id != analysis.bar.identity
            )
        facts = replace(
            facts,
            top_divergence=top_active,
            bottom_divergence=bottom_active,
            p_macd_cross=p_macd,
        )

    decision = step_trend(prior, facts, veto_enabled=bool(rules["h1_veto_enabled"]))
    runtime.trends[event.csymbol] = decision.state
    m15_index = runtime.m15_index_for(event.csymbol)
    runtime.m15_indices[event.csymbol] = m15_index + 1

    gate: GateDecision | None = None
    entry: EntryDecision | None = None
    anchor: AnchorSelection | None = None
    direction = decision.state.trend_state
    permission = None
    evidence_clock = None
    if rules.get('paper_permission_v1'):
        old_permission = runtime.permission_state.get(event.csymbol, {})
        previous_weak = int(old_permission.get('weak_closed_bars', 0)) if old_permission.get('background') == direction else 0
        permission = evaluate_permission(direction, close_side=facts.close_side, slope=facts.slope_state,
            hist=analysis.indicators.hist[-1], opposite_divergence=top_active if direction == 'bull' else bottom_active,
            closed_h1_known=analysis.finalized_h1_absolute is not None,
            calendar_qualified=bool((analysis.h1_source or {}).get('calendar_qualified')),
            previous_weak=previous_weak, changed=decision.confirmed and direction != prior_name,
            eob=analysis.bar.eob.isoformat())
        runtime.permission_state[event.csymbol] = asdict(permission)
    if decision.confirmed and direction != prior_name:
        # A direction key is not an episode identity. Never recycle an old bull/bear N-leg.
        runtime.anchors.pop(_direction_key(event.csymbol, direction),None)
    if direction in ("bull", "bear"):
        typed_direction: Direction = direction
        gate = evaluate_momentum_gate(
            list(event.m15_bars),
            typed_direction,
            enabled=bool(rules["gate_enabled"]),
            distance_atr=float(rules["gate_distance_atr"]),
            shrink_bars=int(rules["gate_hist_shrink_bars"]),
        )
        evidence = build_entry_evidence(
            list(event.m5_bars),
            list(event.m1_bars),
            typed_direction,
            div_mode=str(rules["div_mode"]),  # type: ignore[arg-type]
            m5_lookback=int(rules["m5_flat_lookback"]),
            flat_ratio=float(rules["m5_flat_slope_ratio"]),
            position_atr=float(rules["evidence_position_atr"]),
            pullback_atr=float(rules["pullback_min_atr"]),
            consolidation_bars=int(rules["consolidation_bars"]),
            consolidation_range_atr=float(rules["consolidation_range_atr"]),
            m1_lookback=int(rules["m1_evidence_lookback"]),
        )
        direction_key = _direction_key(event.csymbol, typed_direction)
        atr = analysis.indicators.atr14[-1]
        context = EntryContext(
            direction=typed_direction,
            trend_confirmed=True,
            h1_state=analysis.finalized_h1_absolute,
            m15_slope_state=facts.slope_state,
            m15_reverse_divergence=top_active if typed_direction == "bull" else bottom_active,
            gate_open=gate.is_open,
            same_direction_positions=int(
                event.positions_by_direction.get(typed_direction, event.same_direction_positions)
            ),
            m15_index=m15_index,
            current_price=analysis.bar.close,
            atr14=0.0 if atr is None else float(atr),
            last_same_direction_fill_index=runtime.last_fill_indices.get(direction_key),
            last_same_direction_fill_price=runtime.last_fill_prices.get(direction_key),
            used_evidence_ids=frozenset(runtime.used_evidence_ids),
        )
        entry = evaluate_entries(context, evidence, _entry_settings(rules))
        if entry.accepted and entry.evidence_id:
            # Persist even during warmup: deploying/revisiting a trigger cannot renew it.
            from datetime import timedelta
            evidence_clock = runtime.evidence_clocks.setdefault(event.csymbol+'|'+entry.evidence_id, {
                'evidence_created_at': str(entry.trigger_bar_id).rsplit('|', 1)[-1],
                'first_eligible_at': analysis.bar.eob.isoformat(),
                'expires_at': (analysis.bar.eob + timedelta(seconds=30)).isoformat(),
            })
        pivots = find_pivots(
            list(event.m15_bars),
            analysis.indicators.atr14,
            k=int(rules["pivot_k"]),
            leg_min_atr=float(rules["leg_min_atr"]),
            mode=str(rules["swing_mode"]),  # type: ignore[arg-type]
        )
        anchor = select_anchor(
            list(event.m15_bars),
            typed_direction,
            mode=str(rules["anchor_mode"]),  # type: ignore[arg-type]
            pivots=pivots,
            previous=runtime.anchors.get(direction_key),
        )
        if anchor is not None:
            anchor=replace(anchor,episode_id=decision.state.since_bar_id)
            runtime.anchors[direction_key] = anchor
        elif direction_key in runtime.anchors:
            old_anchor = runtime.anchors.pop(direction_key)
            runtime.anchor_invalidations[str(old_anchor.anchor_bar_id)] = analysis.bar.identity
        if entry.accepted and anchor is None:
            entry = replace(entry, accepted=False, reason="ANCHOR_UNAVAILABLE")
        if entry.accepted and permission is not None and not permission.new_entry_allowed:
            entry = replace(entry, accepted=False, reason='SHARED_PERMISSION_BLOCK:' + ','.join(permission.reasons))

    index = len(analysis.indicators.ema34) - 1
    facts_row = {
        "symbol": event.symbol,
        "csymbol": event.csymbol,
        "eob": analysis.bar.eob.isoformat(),
        "bar_id": facts.bar_id,
        "close": facts.close,
        "ema34": analysis.indicators.ema34[index],
        "atr14": analysis.indicators.atr14[index],
        "slope_state": facts.slope_state,
        "h1_absolute": analysis.h1_absolute,
        "h1_source": analysis.h1_source,
        "trade_permission": asdict(permission) if permission is not None else None,
        "evidence_clock": evidence_clock,
        "top_divergence_event": asdict(stable_top) if stable_top else None,
        "bottom_divergence_event": asdict(stable_bottom) if stable_bottom else None,
        "top_div": facts.top_divergence,
        "bottom_div": facts.bottom_divergence,
        "trend_inputs": {
            "close_side": facts.close_side,
            "s1_extreme_then_flat": facts.s1_extreme_then_flat,
            "s2_bearish_pattern": facts.s2_bearish_pattern,
            "s2_bullish_pattern": facts.s2_bullish_pattern,
            "p_close_cross": facts.p_close_cross,
            "p_macd_cross": facts.p_macd_cross,
            "r_bearish_reverse": facts.r_bearish_reverse,
            "r_bullish_reverse": facts.r_bullish_reverse,
            "h1_relative": facts.h1_state,
            "h1_divergence": facts.h1_divergence,
            "strong_break_atr_met": facts.strong_break_atr_met,
            "unblock_allowed": facts.unblock_allowed,
        },
        "trend_decision": decision.audit(),
    }
    return CoreStepResult(
        analysis, facts, decision, gate, entry, anchor, facts_row, prior_name, m15_index, permission, evidence_clock
    )
