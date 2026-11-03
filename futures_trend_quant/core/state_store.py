from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .models import TrendState


class StateStore:
    """Atomic, fail-closed state persistence under one configured directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        safe = symbol.replace(".", "_").replace("/", "_").replace("\\", "_")
        target = (self.root / f"trend_{safe}.json").resolve()
        if self.root not in target.parents:
            raise ValueError("state path escapes configured root")
        return target

    def load(self, symbol: str) -> TrendState:
        path = self._path(symbol)
        if not path.exists():
            return TrendState()
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1 or raw.get("symbol") != symbol:
            raise ValueError(f"invalid persisted state for {symbol}")
        return TrendState.from_dict(raw["state"])

    def save(self, symbol: str, state: TrendState, *, evidence: dict[str, Any] | None = None) -> Path:
        target = self._path(symbol)
        payload = {
            "schema_version": 1,
            "symbol": symbol,
            "state": state.to_dict(),
            "evidence": evidence or {},
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return target
