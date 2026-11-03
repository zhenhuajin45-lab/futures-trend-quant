"""Finite-state trend confirmation engine."""

from .state_machine import TrendDecision, TrendFacts, step_trend

__all__ = ["TrendDecision", "TrendFacts", "step_trend"]
