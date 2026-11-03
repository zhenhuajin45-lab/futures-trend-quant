"""Replay S0 and historical exit A for the paired ENTRY_H1_V3 engineering audit."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import partial
import gzip
from hashlib import sha256
import json
from pathlib import Path
import tempfile

from futures_trend_quant.config import default_config_path, load_config
from futures_trend_quant.data.development_export import validate_dataset_manifest
from .exit_experiment_strategy import ExitExperimentFactsStrategy
from .exit_rule_experiment import _run_product


GROUPS = ("S0", "A")
BASE_MANIFEST = "5dd4f0588e159f37a1d57ca7cd6c2dbe0e8f5fd09ac41721c85d25e47df2144d"
BASE_CONFIG = "4932fe7258486a022eb32437565c5104994d3d960b7ff3bd05cc70f67714380d"


def replay_product(root, symbol, product, output):
    config = load_config(default_config_path())
    replay = _run_product(str(root), product["continuous_path"], symbol, config.raw["rules"],
                          config.subscription_count, GROUPS,
                          strategy_factory=partial(ExitExperimentFactsStrategy, audit_legacy_h1=True))
    target = output / (symbol + ".jsonl.gz")
    with tempfile.NamedTemporaryFile(dir=output, delete=False) as temp:
        tmp = Path(temp.name)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as stream:
            for i, bar in enumerate(replay["bars"]):
                row = {"date": replay["trading_dates"][i].isoformat(),
                       "bar": {**vars(bar), "bob": bar.bob.isoformat(), "eob": bar.eob.isoformat()},
                       **{group: replay["groups"][group][i] for group in GROUPS}}
                stream.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return symbol, len(replay["bars"])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", required=True, type=Path)
    p.add_argument("--local-output", required=True, type=Path)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()
    root = args.data_root
    manifest = validate_dataset_manifest(root)
    if sha256((root/"manifest.json").read_bytes()).hexdigest() != BASE_MANIFEST:
        raise ValueError("historical manifest mismatch")
    if sha256(default_config_path().read_bytes()).hexdigest() != BASE_CONFIG:
        raise ValueError("historical config mismatch")
    output = args.local_output
    output.mkdir(exist_ok=True, parents=True)
    with ProcessPoolExecutor(max_workers=min(args.workers, len(manifest["products"]))) as pool:
        jobs = [pool.submit(replay_product, root, symbol, product, output)
                for symbol, product in sorted(manifest["products"].items(),
                                              key=lambda item: -item[1]["continuous_m1_rows"])]
        for future in as_completed(jobs):
            print("H1_AUDIT_PRODUCT", *future.result(), flush=True)


if __name__ == "__main__":
    main()
