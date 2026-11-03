from __future__ import annotations

from collections import Counter
import argparse
import json
from pathlib import Path
import re
from typing import Any


LINE = re.compile(r"^\[([A-Z0-9_]+)\]\s+(\{.*\})$")


def parse_log(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    counts: Counter[str] = Counter()
    signal_reasons: Counter[str] = Counter()
    accepted_paths: Counter[str] = Counter()
    guard_reasons: Counter[str] = Counter()
    exit_reasons: Counter[str] = Counter()
    facts: list[dict[str, Any]] = []
    finished: dict[str, Any] | None = None
    malformed = 0
    for raw_line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        match = LINE.match(raw_line.strip())
        if not match:
            continue
        tag, payload_text = match.groups()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            malformed += 1
            continue
        counts[tag] += 1
        if tag == "M15_FACTS":
            facts.append(payload)
        elif tag == "SIGNAL":
            signal_reasons[str(payload.get("reason"))] += 1
            if payload.get("accepted") and payload.get("path"):
                accepted_paths[str(payload["path"])] += 1
        elif tag == "ENTRY_GUARD":
            guard_reasons[str(payload.get("reason"))] += 1
        elif tag == "EXIT_GUARD":
            exit_reasons[str(payload.get("reason"))] += 1
        elif tag == "BACKTEST_FINISHED":
            finished = payload
    facts.sort(key=lambda item: (str(item.get("eob")), str(item.get("symbol"))))
    summary = {
        "schema_version": 1,
        "source": "sanitized GM MODE_BACKTEST log",
        "performance_interpretation_allowed": False,
        "tag_counts": dict(sorted(counts.items())),
        "malformed_json_lines": malformed,
        "m15_fact_rows": len(facts),
        "first_fact_eob": facts[0].get("eob") if facts else None,
        "last_fact_eob": facts[-1].get("eob") if facts else None,
        "signal_reasons": dict(sorted(signal_reasons.items())),
        "accepted_paths": dict(sorted(accepted_paths.items())),
        "entry_guard_reasons": dict(sorted(guard_reasons.items())),
        "exit_reasons": dict(sorted(exit_reasons.items())),
        "backtest_finished": finished,
    }
    return summary, facts


def main() -> int:
    parser = argparse.ArgumentParser(description="Create sanitized structured GM backtest evidence")
    parser.add_argument("log", type=Path)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    args = parser.parse_args()
    summary, facts = parse_log(args.log)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.facts.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with args.facts.open("w", encoding="utf-8", newline="\n") as handle:
        for row in facts:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"summary": str(args.summary), "facts": len(facts)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
