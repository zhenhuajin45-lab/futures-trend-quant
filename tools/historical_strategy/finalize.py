"""Seal no data: write research decision metadata and a reproducibility lock only."""
import argparse
from datetime import datetime,timezone
import importlib.metadata
import platform
from pathlib import Path
import sys
import numpy as np
import pandas as pd
import pyarrow as pa

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,digest,check_lock
from tools.historical_strategy.reproduce import canonical_digest,trace_digest


def main():
    p=argparse.ArgumentParser()
    for name in ("data-root","replay-root","original-root","private-root"):
        p.add_argument("--"+name,type=Path,required=True)
    a=p.parse_args()
    check_lock()
    manifest=read(a.data_root/"manifest.json")
    original=read(a.original_root/"manifest.json")
    assert digest(a.original_root/"manifest.json")==manifest["original_manifest_sha256"]
    n=0
    for product in original["products"].values():
        for item in product["raw_files"]:
            assert digest(a.original_root/item["path"])==item["sha256"]
            n+=1
    data_files={x["path"]:x["research_sha256"] for product in manifest["products"].values() for x in product["contracts"]}
    for name,expected in data_files.items():
        assert digest(a.data_root/name)==expected
    summary_path=ROOT/"reports/v6_price_events_summary.json"
    summary=read(summary_path)
    assert summary["all_contracts_processed"]
    primary=summary["pair_comparison"]["pessimistic"]
    assert summary["matching_coverage"]>=.7 and all(x["high"]<0 for x in primary["intervals"])
    assert all(read(ROOT/"reports/v6_recovery_sensitivity.json")["point_difference_sign_unchanged"].values())
    def execution_gates(value):
        if not isinstance(value,dict):
            return []
        return [v for k,v in value.items() if k=="execute_enabled"]+[gate for v in value.values() for gate in execution_gates(v)]
    gates=execution_gates(read(ROOT/"config/strategy.json"))
    assert gates and all(value is False for value in gates)
    scored=sum(p["selected_complete_m15"] for p in summary["funnels"].values())
    inventoried=sum(p["qualifying_selected_m15"] for p in read(ROOT/"reports/v6_sample_inventory.json")["products"].values())
    assert scored==inventoried,"unattributed qualified M15 loss"
    if not (a.private_root/"summary_before_decision.json").exists():
        write(a.private_root/"summary_before_decision.json",summary)
    decision=dict(scope="V6_RESEARCH_ONLY; NO_FORWARD_APPROVAL_OR_TRADING_PERMISSION",
        t3_status="STOPPED_NO_POSITIVE_ENTRY_ENRICHMENT",strategy_parameters_changed=False,
        m5_readiness_counterfactual="NOT_RUN; NO_CLAIM_THAT_EARLIER_LEGAL_SIGNALS_NEVER_EXIST",
        next_single_bottleneck="ENTRY_SELECTION_INCREMENTAL_VALUE",
        rationale="S0 entry timing underperforms preregistered same-direction matched ordinary times",
        complete_pairs=primary["n"],difference=primary["difference"],intervals=primary["intervals"],
        sample_status="PREVIOUSLY_OBSERVED_EXPLORATORY",net_performance_evaluated=False,
        main_summary_before_decision_sha256=digest(a.private_root/"summary_before_decision.json"))
    write(ROOT/"reports/v6_t3_decision.json",decision)
    summary.update(t3_status=decision["t3_status"],research_decision_artifact="reports/v6_t3_decision.json")
    write(summary_path,summary)
    source_paths=set((ROOT/"futures_trend_quant/core").rglob("*.py"))
    source_paths.update(ROOT/name for name in ("config/strategy.json","backtest/aggregation.py","backtest/exit_experiment_strategy.py",
        "backtest/exit_rule_variants.py","tools/historical_strategy/analyze.py","tools/historical_strategy/price_paths.py",
        "tools/historical_strategy/inventory.py","tools/historical_strategy/replay.py","tools/historical_strategy/registration.json",
        "tools/historical_strategy/reproduce.py"))
    lock=dict(data_manifest_sha256=digest(a.data_root/"manifest.json"),data_file_sha256=data_files,
        canonical_source_sha256={p.relative_to(ROOT).as_posix():canonical_digest(p) for p in sorted(source_paths)},
        replay_jsonl_sha256={t["symbol"]+".jsonl.gz":trace_digest(a.replay_root/(t["symbol"]+".jsonl.gz")) for t in manifest["tasks"]},
        canonicalization="UTF8_TEXT_UNIVERSAL_NEWLINES; GZIP_CONTAINER_METADATA_EXCLUDED")
    write(ROOT/"reports/v6_reproduction_lock.json",lock)
    if not (a.private_root/"execution_receipt_local.json").exists():
        write(a.private_root/"execution_receipt_local.json",read(ROOT/"reports/v6_execution_receipt.json"))
    receipt=read(a.private_root/"execution_receipt_local.json")
    for phase in receipt["phases"]:
        phase["arguments"]=[value.replace(str(ROOT),"${RESEARCH_ROOT}") for value in phase["arguments"]]
    receipt.update(analysis_pipeline_status=receipt["status"],status="V6_RESEARCH_REVIEW_PACKAGE_COMPLETE",
        postrun_research_decision=decision["t3_status"],local_paths_redacted=True,
        local_full_receipt_sha256=digest(a.private_root/"execution_receipt_local.json"))
    write(ROOT/"reports/v6_execution_receipt.json",receipt)
    write(ROOT/"reports/v6_input_manifest.json",dict(baseline_commit="316d0ed0599903276108cf2bbb84a4c5c9182725",
        preregistration_commit="16b4d29c48406f2f2e7053ca1ca74bf181c9e705",
        data_manifest_sha256=lock["data_manifest_sha256"],original_manifest_sha256=manifest["original_manifest_sha256"],
        original_contract_files_verified=n,research_contract_files_verified=len(data_files),replayed_contracts=len(manifest["tasks"]),
        scored_m15=scored,inventory_qualified_m15=inventoried,
        environment=dict(python=platform.python_version(),pandas=pd.__version__,numpy=np.__version__,pyarrow=pa.__version__),
        historical_mapping_publication="UNKNOWN",historical_arrival_clock="UNKNOWN",originals_immutable=True,
        formal_protocol_hash_check="PASS",execute_enabled_values=gates,
        completed_at=datetime.now(timezone.utc).isoformat()))
    print("V6_RESEARCH_FINALIZED",n,len(data_files),len(manifest["tasks"]),flush=True)


if __name__=="__main__":
    main()
