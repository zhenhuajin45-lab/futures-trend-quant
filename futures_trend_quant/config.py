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

    risk_pct = float(raw.get("risk", {}).get("risk_pct", 0))
    _require(0 < risk_pct <= 0.01, "risk_pct must be in (0, 0.01]")
    risk_cap_abs = raw["risk"].get("risk_cap_abs")
    _require(risk_cap_abs is None or float(risk_cap_abs) > 0, "risk_cap_abs must be null or positive")
    _require(0 < float(raw["risk"]["margin_usage_pct"]) <= 0.8, "margin_usage_pct must be in (0, 0.8]")
    _require(0 <= float(raw["risk"]["market_gap_buffer_bps"]) <= 50, "market_gap_buffer_bps must be in [0, 50]")
    _require(float(raw["risk"]["product_daily_loss_pct"]) <= 0.02, "product daily loss cap exceeds 2%")
    _require(float(raw["risk"]["portfolio_daily_loss_pct"]) <= 0.06, "portfolio daily loss cap exceeds 6%")

    frequencies = tuple(raw.get("data", {}).get("frequencies", ()))
    _require(frequencies == ("60s", "300s", "900s"), "frequencies must be exactly 60s/300s/900s")
    _require(int(raw["data"]["subscription_count"]) >= int(raw["data"]["indicator_warmup_bars"]), "subscription window is shorter than warmup")

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
