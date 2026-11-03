from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


SPRINT2_DOMAINS: dict[str, tuple[object, ...]] = {
    "rules.div_mode": ("A47", "B_hist"),
    "rules.swing_mode": ("S_A", "S_B"),
    "rules.h1_veto_enabled": (True, False),
    "rules.h1_strong_atr": (0.4, 0.6, 0.8),
    "rules.stop_max_atr": (6.0, 11.5),
    "rules.m1_confirmation_enabled": (False, True),
    "rules.gate_enabled": (True, False),
    "rules.cooldown_bars": (0, 4),
    "rules.pm_mode": ("fixed", "be_at_1r"),
    "rules.anchor_mode": ("swing", "range20"),
}

SPRINT2_UNSUPPORTED = {
    "rules.pm_mode=be_at_1r": "break-even position management is not implemented in both GM and offline adapters",
}


@dataclass(frozen=True)
class AblationCase:
    name: str
    key_path: str
    baseline: object
    candidate: object
    config: dict[str, Any]


def _walk_leaves(value: Mapping[str, Any], prefix: str = ""):
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, Mapping):
            yield from _walk_leaves(item, path)
        else:
            yield path, item


def _set_path(config: dict[str, Any], key_path: str, value: object) -> None:
    parts = key_path.split(".")
    cursor = config
    for part in parts[:-1]:
        cursor = cursor[part]
    cursor[parts[-1]] = value


def generate_ablation_cases(
    baseline: Mapping[str, Any],
    *,
    domains: Mapping[str, Iterable[object]] | None = None,
) -> list[AblationCase]:
    """Generate one-at-a-time boolean and declared-enum changes."""

    domain_map = {key: tuple(values) for key, values in (domains or {}).items()}
    cases: list[AblationCase] = []
    for path, value in _walk_leaves(baseline):
        candidates: tuple[object, ...]
        if isinstance(value, bool):
            candidates = (not value,)
        else:
            candidates = tuple(item for item in domain_map.get(path, ()) if item != value)
        for candidate in candidates:
            config = deepcopy(dict(baseline))
            _set_path(config, path, candidate)
            cases.append(
                AblationCase(
                    name=f"{path}={candidate}",
                    key_path=path,
                    baseline=value,
                    candidate=candidate,
                    config=config,
                )
            )
    return cases


def flatten_metrics(case: AblationCase, metrics: Mapping[str, object]) -> dict[str, object]:
    return {
        "case": case.name,
        "key_path": case.key_path,
        "baseline": case.baseline,
        "candidate": case.candidate,
        **metrics,
    }


def generate_sprint2_cases(baseline: Mapping[str, Any]) -> list[AblationCase]:
    """Generate only the pre-registered one-at-a-time Sprint 2 cases."""

    cases = generate_ablation_cases(baseline, domains=SPRINT2_DOMAINS)
    allowed = set(SPRINT2_DOMAINS)
    return [case for case in cases if case.key_path in allowed]


def ablation_support(case: AblationCase) -> tuple[bool, str]:
    reason = SPRINT2_UNSUPPORTED.get(case.name)
    return (reason is None, "SUPPORTED" if reason is None else reason)
