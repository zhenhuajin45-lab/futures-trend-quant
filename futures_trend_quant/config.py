from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a runtime configuration violates a fail-closed rule."""


@dataclass(frozen=True)
class Product:
    name: str
    exchange: str
    product: str
    csymbol: str
    cluster: str


@dataclass(frozen=True)
class StrategyConfig:
    path: Path
    raw: dict[str, Any]
    products: tuple[Product, ...]

    @property
    def execute_enabled(self) -> bool:
        return bool(self.raw["execution"]["execute_enabled"])

    @property
    def backtest_orders_enabled(self) -> bool:
        return bool(self.raw["execution"]["backtest_orders_enabled"])

    @property
    def execution_mode(self) -> str:
        return str(self.raw["execution"]["mode"])

    @property
    def risk_pct(self) -> float:
        return float(self.raw["risk"]["risk_pct"])

    @property
    def frequencies(self) -> tuple[str, ...]:
        return tuple(self.raw["data"]["frequencies"])

    @property
    def subscription_count(self) -> int:
        return int(self.raw["data"]["subscription_count"])

    @property
    def initial_cash(self) -> float:
        return float(self.raw["backtest"]["initial_cash"])


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def load_config(path: str | Path) -> StrategyConfig:
    config_path = Path(path).resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))

    _require(raw.get("schema_version") == 1, "unsupported schema_version")
    execution = raw.get("execution", {})
    _require(execution.get("mode") in ("signal_only", "backtest"), "unsupported execution mode")
    _require(raw.get("execution", {}).get("execute_enabled") is False, "execute_enabled must remain false")
    backtest_enabled = execution.get("backtest_orders_enabled")
    _require(isinstance(backtest_enabled, bool), "backtest_orders_enabled must be boolean")
    _require(not backtest_enabled or execution.get("mode") == "backtest", "backtest orders require backtest mode")

    backtest = raw.get("backtest", {})
    _require(float(backtest.get("initial_cash", 0)) == 1_000_000, "backtest initial_cash must be 1,000,000")
    _require(int(backtest.get("slippage_ticks", 0)) >= 1, "slippage_ticks must be at least one tick")
    _require(backtest.get("rollover_policy") in ("hold", "roll_at_close"), "unsupported rollover_policy")

    risk_pct = float(raw.get("risk", {}).get("risk_pct", 0))
    _require(0 < risk_pct <= 0.01, "risk_pct must be in (0, 0.01]")
    risk_cap_abs = raw["risk"].get("risk_cap_abs")
    _require(risk_cap_abs is None or float(risk_cap_abs) > 0, "risk_cap_abs must be null or positive")
    _require(0 < float(raw["risk"]["margin_usage_pct"]) <= 0.8, "margin_usage_pct must be in (0, 0.8]")
    _require(0 <= float(raw["risk"]["market_gap_buffer_bps"]) <= 50, "market_gap_buffer_bps must be in [0, 50]")
    _require(float(raw["risk"]["product_daily_loss_pct"]) <= 0.02, "product daily loss cap exceeds 2%")
    _require(float(raw["risk"]["portfolio_daily_loss_pct"]) <= 0.06, "portfolio daily loss cap exceeds 6%")
    _require(int(raw["risk"]["max_open_positions"]) > 0, "max_open_positions must be positive")
    _require(int(raw["risk"]["max_positions_per_product"]) > 0, "max_positions_per_product must be positive")
    _require(int(raw["risk"]["max_positions_per_cluster"]) > 0, "max_positions_per_cluster must be positive")
    _require(0 < float(raw["risk"]["adv_participation_limit"]) <= 0.001, "adv participation limit exceeds 0.1%")

    frequencies = tuple(raw.get("data", {}).get("frequencies", ()))
    _require(frequencies == ("60s", "300s", "900s"), "frequencies must be exactly 60s/300s/900s")
    _require(int(raw["data"]["subscription_count"]) >= 1200, "subscription window must contain at least 1200 M15 bars")
    _require(int(raw["data"]["subscription_count"]) >= int(raw["data"]["indicator_warmup_bars"]), "subscription window is shorter than warmup")
    _require(raw["data"].get("h1_mode") == "sequential_m15x4", "h1_mode must preserve sequential M15 coverage")
    _require(raw["data"].get("continuous_adjustment") == "self_back_adjust", "continuous adjustment must be self_back_adjust")

    rules = raw.get("rules", {})
    bool_keys = (
        "h1_veto_enabled",
        "pending_divergence_enabled",
        "gate_enabled",
        "path_a_enabled",
        "path_b_enabled",
        "path_c_enabled",
        "path_d_enabled",
        "path_b_h1_exception",
        "m1_confirmation_enabled",
    )
    _require(all(isinstance(rules.get(key), bool) for key in bool_keys), "rule switches must be boolean")
    _require(any(rules[key] for key in ("path_a_enabled", "path_b_enabled", "path_c_enabled", "path_d_enabled")), "at least one entry path must be enabled")
    _require(rules.get("divergence_validity_mode") in ("extreme", "ttl"), "unsupported divergence_validity_mode")
    _require(int(rules.get("divergence_ttl_bars", 0)) > 0, "divergence_ttl_bars must be positive")
    _require(int(rules.get("gate_hist_shrink_bars", 0)) >= 2, "gate_hist_shrink_bars must be at least two")
    _require(float(rules.get("gate_distance_atr", 0)) > 0, "gate_distance_atr must be positive")
    _require(int(rules.get("cooldown_bars", -1)) >= 0, "cooldown_bars must be non-negative")
    _require(float(rules.get("path_d_cooldown_atr", 0)) > 0, "path_d_cooldown_atr must be positive")
    _require(int(rules.get("path_d_max_stack", 0)) in (1, 2), "path_d_max_stack must be one or two")
    _require(int(rules["path_d_max_stack"]) <= int(raw["risk"]["max_positions_per_product"]), "path D stack exceeds product position cap")
    _require(rules.get("anchor_mode") in ("swing", "range20"), "unsupported anchor_mode")
    _require(rules.get("pm_mode") in ("fixed", "be_at_1r"), "unsupported pm_mode")

    items = raw.get("universe")
    _require(isinstance(items, list) and items, "universe must not be empty")
    products = tuple(Product(**item) for item in items)
    _require(len({item.csymbol for item in products}) == len(products), "duplicate csymbol")
    _require(len({(item.exchange, item.product) for item in products}) == len(products), "duplicate exchange/product")
    _require(all(item.csymbol == f"{item.exchange}.{item.product}" for item in products), "invalid csymbol mapping")
    _require(len(products) <= 10, "universe exceeds the 10-product limit")

    return StrategyConfig(path=config_path, raw=raw, products=products)


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "strategy.json"
