from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping

from futures_trend_quant.core.entry import (
    AnchorSelection,
    EntryContext,
    EntryDecision,
    build_entry_evidence,
    evaluate_entries,
    evaluate_momentum_gate,
    select_anchor,
)
from futures_trend_quant.core.entry.paths import entry_h1_reasons
from futures_trend_quant.core.facts.h1_state import relative_h1_state
from futures_trend_quant.core.facts.pipeline import MarketAnalysis, analyze_market, trend_facts
from futures_trend_quant.core.facts.swing import find_pivots
from futures_trend_quant.core.facts_state import latest_valid_divergence, update_facts_state
from futures_trend_quant.core.models import Direction
from futures_trend_quant.core.pipeline import CoreStepInput, StrategyRuntimeBook, run_core_step
from futures_trend_quant.core.pipeline.step import _entry_settings

from .exit_rule_variants import ExperimentGroup, step_experiment_trend
from .runner import StepInput, StepOutput


def _direction_key(csymbol: str, direction: Direction) -> str:
    return f"{csymbol}|{direction}"


def _entry_result(entry: EntryDecision) -> dict[str, object]:
    return {
        "accepted": entry.accepted,
        "path": entry.path,
        "reason": entry.reason,
        "trigger_bar_id": entry.trigger_bar_id,
        "evidence_id": entry.evidence_id,
        "formed_paths": [candidate.path for candidate in entry.candidates],
    }


def _entry_audit(
    entry: EntryDecision, gate_open: bool, anchor: AnchorSelection | None,
    analysis: MarketAnalysis, prior: str, trend_after: str, path_b_exception: bool,
) -> dict[str, object]:
    absolute = analysis.finalized_h1_absolute
    source_eob = analysis.finalized_h1_eob
    b, d = entry_h1_reasons(absolute, entry.direction, path_b_exception)
    return {
        **_entry_result(entry),
        "schema": "ENTRY_H1_V3",
        "h1_absolute": absolute,
        "h1_source_eob": source_eob.isoformat() if source_eob is not None else None,
        "prior_state": prior,
        "trend_after": trend_after,
        "entry_direction": entry.direction,
        "h1_relative": relative_h1_state(absolute, entry.direction) if absolute is not None else "UNKNOWN",
        "path_b_h1_reason": b,
        "path_d_h1_reason": d,
        "gate_open": gate_open,
        "anchor_available": anchor is not None,
        "anchor": None if anchor is None else asdict(anchor),
    }


@dataclass
class ExitExperimentFactsStrategy:
    """Research-only adapter that keeps one independent runtime per group."""

    group: ExperimentGroup
    rules: Mapping[str, object]
    csymbol_by_symbol: Mapping[str, str]
    runtime: StrategyRuntimeBook = field(default_factory=StrategyRuntimeBook)
    audit_legacy_h1: bool = False

    def __call__(self, event: StepInput) -> StepOutput:
        csymbol = self.csymbol_by_symbol.get(event.symbol)
        if csymbol is None:
            raise ValueError(f"missing csymbol mapping for {event.symbol}")
        candidate_runtime = StrategyRuntimeBook.from_dict(self.runtime.to_dict())
        try:
            output = self._run_step(event, csymbol, candidate_runtime)
        except ValueError as exc:
            message = str(exc).lower()
            if not any(marker in message for marker in ("warmup", "unavailable", "requires")):
                raise
            return StepOutput(
                facts={
                    "symbol": event.symbol,
                    "csymbol": csymbol,
                    "eob": event.m15_bar.eob.isoformat(),
                    "experiment_group": self.group,
                },
                blocked_at="FACTS_WARMUP",
            )
        self.runtime = candidate_runtime
        return output

    def _run_step(
        self,
        event: StepInput,
        csymbol: str,
        runtime: StrategyRuntimeBook,
    ) -> StepOutput:
        core_input = CoreStepInput(
            csymbol=csymbol,
            symbol=event.symbol,
            m15_bars=event.m15_bars,
            m5_bars=event.m5_bars,
            m1_bars=event.m1_bars,
            rules=self.rules,
        )
        if self.group == "S0" and not self.audit_legacy_h1:
            result = run_core_step(core_input, runtime)
            row = dict(result.facts_row)
            row.update({
                "high": result.analysis.bar.high,
                "low": result.analysis.bar.low,
                "experiment_group": self.group,
                "entry_evaluation": (
                    None
                    if result.entry is None
                    else _entry_audit(
                        result.entry, result.gate.is_open, result.anchor, result.analysis,
                        result.prior_trend, result.trend.confirmed_state, bool(self.rules["path_b_h1_exception"]),
                    )
                ),
            })
            return StepOutput(
                facts=row,
                blocked_at=None if result.entry is None else result.entry.reason,
            )

        facts_state = runtime.facts.for_csymbol(csymbol)
        update_facts_state(facts_state, list(event.m15_bars), self.rules)
        analysis = analyze_market(list(event.m15_bars), self.rules)
        return self.run_prepared(
            event, csymbol, runtime, analysis,
            latest_valid_divergence(facts_state, list(event.m15_bars), "top"),
            latest_valid_divergence(facts_state, list(event.m15_bars), "bottom"), {},
        )

    def run_prepared(
        self,
        event: StepInput,
        csymbol: str,
        runtime: StrategyRuntimeBook,
        analysis: MarketAnalysis,
        stable_top: object | None,
        stable_bottom: object | None,
        shared_cache: dict[object, Any],
    ) -> StepOutput:
        """Advance one group from state-independent market analysis.

        The caller owns one runtime per group.  Only indicators, pivots, gate
        results, and direction-specific entry evidence are shared.
        """

        prior = runtime.trend_for(csymbol)
        prior_name = prior.trend_state
        facts = trend_facts(analysis, prior)
        top_active = stable_top is not None
        bottom_active = stable_bottom is not None
        if top_active != facts.top_divergence or bottom_active != facts.bottom_divergence:
            p_macd = facts.p_macd_cross
            if prior_name == "bull":
                p_macd = bool(
                    stable_top
                    and analysis.macd_cross == "down"
                    and getattr(stable_top, "defined_at_bar_id") != analysis.bar.identity
                )
            elif prior_name == "bear":
                p_macd = bool(
                    stable_bottom
                    and analysis.macd_cross == "up"
                    and getattr(stable_bottom, "defined_at_bar_id") != analysis.bar.identity
                )
            facts = replace(
                facts,
                top_divergence=top_active,
                bottom_divergence=bottom_active,
                p_macd_cross=p_macd,
            )

        decision = step_experiment_trend(
            prior,
            facts,
            group=self.group,
            veto_enabled=bool(self.rules["h1_veto_enabled"]),
        )
        runtime.trends[csymbol] = decision.state
        m15_index = runtime.m15_index_for(csymbol)
        runtime.m15_indices[csymbol] = m15_index + 1
        entry = None
        entry_evaluation = None
        anchor = None
        if self.group != "EMA34_CONTROL" and decision.state.trend_state in ("bull", "bear"):
            direction: Direction = decision.state.trend_state
            gate_key = ("gate", direction)
            if gate_key not in shared_cache:
                shared_cache[gate_key] = evaluate_momentum_gate(
                    list(event.m15_bars),
                    direction,
                    enabled=bool(self.rules["gate_enabled"]),
                    distance_atr=float(self.rules["gate_distance_atr"]),
                    shrink_bars=int(self.rules["gate_hist_shrink_bars"]),
                )
            evidence_key = ("evidence", direction)
            if evidence_key not in shared_cache:
                shared_cache[evidence_key] = build_entry_evidence(
                    list(event.m5_bars),
                    list(event.m1_bars),
                    direction,
                    div_mode=str(self.rules["div_mode"]),
                    m5_lookback=int(self.rules["m5_flat_lookback"]),
                    flat_ratio=float(self.rules["m5_flat_slope_ratio"]),
                    position_atr=float(self.rules["evidence_position_atr"]),
                    pullback_atr=float(self.rules["pullback_min_atr"]),
                    consolidation_bars=int(self.rules["consolidation_bars"]),
                    consolidation_range_atr=float(self.rules["consolidation_range_atr"]),
                    m1_lookback=int(self.rules["m1_evidence_lookback"]),
                )
            if "pivots" not in shared_cache:
                shared_cache["pivots"] = find_pivots(
                    list(event.m15_bars),
                    analysis.indicators.atr14,
                    k=int(self.rules["pivot_k"]),
                    leg_min_atr=float(self.rules["leg_min_atr"]),
                    mode=str(self.rules["swing_mode"]),
                )
            direction_key = _direction_key(csymbol, direction)
            atr_value = analysis.indicators.atr14[-1]
            context = EntryContext(
                direction=direction,
                trend_confirmed=True,
                h1_state=analysis.finalized_h1_absolute,
                m15_slope_state=facts.slope_state,
                m15_reverse_divergence=top_active if direction == "bull" else bottom_active,
                gate_open=shared_cache[gate_key].is_open,
                same_direction_positions=0,
                m15_index=m15_index,
                current_price=analysis.bar.close,
                atr14=0.0 if atr_value is None else float(atr_value),
                last_same_direction_fill_index=runtime.last_fill_indices.get(direction_key),
                last_same_direction_fill_price=runtime.last_fill_prices.get(direction_key),
                used_evidence_ids=frozenset(runtime.used_evidence_ids),
            )
            settings = _entry_settings(self.rules)
            entry = evaluate_entries(context, shared_cache[evidence_key], settings)
            anchor = select_anchor(
                list(event.m15_bars),
                direction,
                mode=str(self.rules["anchor_mode"]),
                pivots=shared_cache["pivots"],
                previous=runtime.anchors.get(direction_key),
            )
            if anchor is not None:
                runtime.anchors[direction_key] = anchor
            if entry.accepted and anchor is None:
                entry = replace(entry, accepted=False, reason="ANCHOR_UNAVAILABLE")
            entry_evaluation = _entry_audit(
                entry, context.gate_open, anchor, analysis, prior_name,
                decision.confirmed_state, settings.path_b_h1_exception,
            )
            if self.audit_legacy_h1:
                # 52301f1 passed a relative string to both gates. B never saw an
                # opposite strong absolute and D accepted only the literal "strong".
                legacy_mask = f"{direction}_strong" if facts.h1_state == "strong" else "flat"
                legacy = evaluate_entries(replace(context, h1_state=legacy_mask), shared_cache[evidence_key], settings)
                if legacy.accepted and anchor is None:
                    legacy = replace(legacy, accepted=False, reason="ANCHOR_UNAVAILABLE")
                entry_evaluation["legacy_h1"] = {
                    **_entry_result(legacy), "input_relative": facts.h1_state, "schema": "ENTRY_H1_52301F1",
                }

        indicator_index = len(analysis.indicators.ema34) - 1
        row = {
            "symbol": event.symbol,
            "csymbol": csymbol,
            "eob": analysis.bar.eob.isoformat(),
            "bar_id": facts.bar_id,
            "close": facts.close,
            "high": facts.high,
            "low": facts.low,
            "ema34": analysis.indicators.ema34[indicator_index],
            "atr14": analysis.indicators.atr14[indicator_index],
            "slope_state": facts.slope_state,
            "h1_absolute": analysis.h1_absolute,
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
            "experiment_group": self.group,
            "entry_evaluation": entry_evaluation,
        }
        return StepOutput(
            facts=row,
            blocked_at=None if entry is None else entry.reason,
        )
