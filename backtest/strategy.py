from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from futures_trend_quant.core.pipeline import (
    CoreStepInput,
    StrategyRuntimeBook,
    run_core_step,
)
from futures_trend_quant.core.risk import ContractSpec, build_stop_plan, calculate_size

from .models import SignalIntent
from .runner import StepInput, StepOutput


@dataclass
class SharedCoreStrategy:
    """Offline adapter around the exact core step used by the GM callback."""

    rules: Mapping[str, object]
    csymbol_by_symbol: Mapping[str, str]
    specs_by_symbol: Mapping[str, ContractSpec]
    max_lots_by_product: Mapping[str, int]
    risk: Mapping[str, object]
    initial_equity: float = 1_000_000.0
    runtime: StrategyRuntimeBook = field(default_factory=StrategyRuntimeBook)

    def __post_init__(self) -> None:
        if self.initial_equity != 1_000_000:
            raise ValueError("Sprint 1 baseline equity must be 1,000,000")

    def __call__(self, event: StepInput) -> StepOutput:
        csymbol = self.csymbol_by_symbol.get(event.symbol)
        if csymbol is None:
            raise ValueError(f"missing csymbol mapping for {event.symbol}")
        for position in event.positions:
            if position.evidence_id and position.evidence_id not in self.runtime.used_evidence_ids:
                self.runtime.record_fill(
                    csymbol=csymbol,
                    direction="bull" if position.side == "long" else "bear",
                    price=position.entry_price,
                    evidence_id=position.evidence_id,
                    m15_index=position.signal_m15_index,
                )
        counts = {
            "bull": sum(position.side == "long" for position in event.positions),
            "bear": sum(position.side == "short" for position in event.positions),
        }
        candidate_runtime = StrategyRuntimeBook.from_dict(self.runtime.to_dict())
        try:
            result = run_core_step(
                CoreStepInput(
                    csymbol=csymbol,
                    symbol=event.symbol,
                    m15_bars=event.m15_bars,
                    m5_bars=event.m5_bars,
                    m1_bars=event.m1_bars,
                    rules=self.rules,
                    positions_by_direction=counts,
                ),
                candidate_runtime,
            )
        except ValueError as exc:
            message = str(exc).lower()
            if not any(marker in message for marker in ("warmup", "unavailable", "requires")):
                raise
            return StepOutput(
                facts={
                    "symbol": event.symbol,
                    "csymbol": csymbol,
                    "eob": event.m15_bar.eob.isoformat(),
                },
                blocked_at="FACTS_WARMUP",
            )
        self.runtime = candidate_runtime
        close_reason = None
        if result.trend.confirmed and any(
            result.trend.confirmed_state != ("bull" if position.side == "long" else "bear")
            for position in event.positions
        ):
            close_reason = "TREND_EXIT"
        entry = result.entry
        if entry is None or not entry.accepted or entry.path is None or entry.evidence_id is None:
            return StepOutput(
                facts=result.facts_row,
                blocked_at=None if entry is None else entry.reason,
                close_reason=close_reason,
            )
        spec = self.specs_by_symbol.get(event.symbol)
        if spec is None:
            return StepOutput(facts=result.facts_row, blocked_at="SPEC_MISSING", close_reason=close_reason)
        product = _product_from_symbol(event.symbol)
        max_lots = self.max_lots_by_product.get(product)
        if max_lots is None or max_lots <= 0:
            return StepOutput(
                facts=result.facts_row,
                blocked_at="ADV_CAP_UNAVAILABLE",
                close_reason=close_reason,
            )
        if result.anchor is None:
            return StepOutput(facts=result.facts_row, blocked_at="ANCHOR_UNAVAILABLE", close_reason=close_reason)
        atr = result.analysis.indicators.atr14[-1]
        if atr is None:
            return StepOutput(facts=result.facts_row, blocked_at="ATR_UNAVAILABLE", close_reason=close_reason)
        try:
            stop = build_stop_plan(
                direction=entry.direction,
                entry=result.analysis.bar.close,
                anchor=result.anchor.anchor_price,
                atr=float(atr),
                tick=spec.price_tick,
                buffer_atr=float(self.rules["stop_buffer_atr"]),
                min_atr=float(self.rules["stop_min_atr"]),
                max_atr=float(self.rules["stop_max_atr"]),
            )
            cushion = max(
                3 * spec.price_tick,
                result.analysis.bar.close * float(self.risk["market_gap_buffer_bps"]) / 10_000,
            )
            size = calculate_size(
                equity=self.initial_equity,
                available=self.initial_equity,
                entry=result.analysis.bar.close,
                stop_distance=stop.distance + cushion,
                spec=spec,
                risk_pct=float(self.risk["risk_pct"]),
                risk_cap_abs=(
                    None if self.risk["risk_cap_abs"] is None else float(self.risk["risk_cap_abs"])
                ),
                margin_usage_pct=float(self.risk["margin_usage_pct"]),
            )
        except ValueError as exc:
            return StepOutput(
                facts=result.facts_row,
                blocked_at=f"RISK_REJECT:{exc}",
                close_reason=close_reason,
            )
        volume = min(size.volume, max_lots)
        if volume <= 0:
            return StepOutput(facts=result.facts_row, blocked_at="ZERO_VOLUME", close_reason=close_reason)
        return StepOutput(
            signal=SignalIntent(
                symbol=event.symbol,
                side="long" if entry.direction == "bull" else "short",
                volume=volume,
                stop_price=stop.stop,
                target_price=stop.target,
                path=entry.path,
                created_at=result.analysis.bar.eob,
                allow_stack=entry.path == "D",
                evidence_id=entry.evidence_id,
                signal_m15_index=result.m15_index,
            ),
            facts=result.facts_row,
            close_reason=close_reason,
        )


def _product_from_symbol(symbol: str) -> str:
    tail = symbol.split(".")[-1]
    product = "".join(character for character in tail if character.isalpha()).upper()
    if not product:
        raise ValueError(f"cannot derive product from {symbol!r}")
    return product
