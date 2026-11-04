"""One-off local historical job: finish completed replays, audit, never publish."""
import argparse
from datetime import datetime,timezone
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,digest,check_lock


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--data-root",type=Path,required=True)
    parser.add_argument("--replay-root",type=Path,required=True)
    parser.add_argument("--without-data-root",type=Path,required=True)
    parser.add_argument("--without-replay-root",type=Path,required=True)
    parser.add_argument("--local-output",type=Path,required=True)
    parser.add_argument("--wait-max-minutes",type=int,default=120)
    a=parser.parse_args()
    check_lock()
    receipt=ROOT/"reports/v6_execution_receipt.json"
    hashes={name:digest(ROOT/"tools/historical_strategy"/name) for name in
            ("analyze.py","price_paths.py","inventory.py","audit_cases.py","replay.py","registration.json","compare_patch.py")}
    record=dict(study="HISTORICAL_2R_V6",started_at=datetime.now(timezone.utc).isoformat(),code_sha256=hashes,
                data_manifest_sha256=digest(a.data_root/"manifest.json"),phases=[],no_orders=True,no_external_publication=True)
    deadline=time.monotonic()+a.wait_max_minutes*60
    def wait_for(root,replays,phase):
        tasks=read(root/"manifest.json")["tasks"]
        last=None
        while True:
            count=sum((replays/(x["symbol"]+".jsonl.gz")).exists() for x in tasks)
            if count!=last:
                print(phase,count,len(tasks),flush=True)
                record.update(status=phase,completed_replays=count,expected_replays=len(tasks))
                write(receipt,record)
                last=count
            if count==len(tasks):
                return
            if time.monotonic()>deadline:
                raise TimeoutError("local replay deadline reached")
            time.sleep(15)
    def run(name,arguments):
        command=[sys.executable,str(ROOT/"tools/historical_strategy"/(name+".py")),*map(str,arguments)]
        result=subprocess.run(command,cwd=ROOT,check=False)
        record["phases"].append(dict(script=name,arguments=list(map(str,arguments)),exit_code=result.returncode))
        write(receipt,record)
        if result.returncode:
            raise RuntimeError("local historical stage failed: "+name)
    try:
        wait_for(a.data_root,a.replay_root,"WAITING_MAIN_REPLAYS")
        run("analyze",["--data-root",a.data_root,"--replay-root",a.replay_root,"--local-output",a.local_output,
                       "--summary",ROOT/"reports/v6_price_events_summary.json"])
        assert read(ROOT/"reports/v6_price_events_summary.json")["all_contracts_processed"]
        run("audit_cases",["--data-root",a.data_root,"--analysis-root",a.local_output,
                           "--summary",ROOT/"reports/v6_hand_checks.json"])
        wait_for(a.without_data_root,a.without_replay_root,"WAITING_PATCH_REPLAYS")
        run("compare_patch",["--data-root",a.data_root,"--replay-root",a.replay_root,
            "--without-data-root",a.without_data_root,"--without-replay-root",a.without_replay_root,
            "--summary",ROOT/"reports/v6_patch_signal_impact.json"])
        for name,expected in hashes.items():
            assert digest(ROOT/"tools/historical_strategy"/name)==expected,"analysis code changed during job"
        check_lock()
        record.update(status="PRICE_EVENTS_COMPLETE_AWAITING_STRATEGY_DECISION",finished_at=datetime.now(timezone.utc).isoformat())
        write(receipt,record)
        print(record["status"],flush=True)
    except BaseException as exc:
        record.update(status="FAILED",error_type=type(exc).__name__,error=str(exc))
        write(receipt,record)
        raise


if __name__=="__main__":
    main()
