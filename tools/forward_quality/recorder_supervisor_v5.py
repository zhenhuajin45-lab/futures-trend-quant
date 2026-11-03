"""Keep the authorized raw evidence process alive until its fixed collection end."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

CN=timezone(timedelta(hours=8))


def main():
    root=Path(sys.argv[1]).resolve()
    root.mkdir(exist_ok=True,parents=True)
    guard=root/"supervisor.lock"
    with guard.open("x") as file:
        file.write(str(os.getpid()))
    stop=datetime.fromisoformat("2026-11-05T15:05:00+08:00")
    try:
        while datetime.now(CN)<stop:
            run_id=datetime.now(CN).strftime("%Y%m%d_%H%M%S")+"_"+uuid4().hex[:8]
            env=os.environ.copy()
            env["FTQ_RECORD_RUN"]=str(root/run_id)
            env["FTQ_RECORD_STOP"]=stop.isoformat()
            env["FTQ_RECORD_STRATEGY_ID"]=str(uuid4())
            with (root/(run_id+".stdout.log")).open("xb") as output:
                child=subprocess.Popen([sys.executable,str(Path(__file__).with_name("recorder_v5.py"))],
                                       env=env,stdout=output,stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                (root/"supervisor.json").write_text(json.dumps(dict(supervisor_pid=os.getpid(),child_pid=child.pid,
                    run_id=run_id,started_at=datetime.now(CN).isoformat(),stop_at=stop.isoformat())),encoding="utf8")
                result=child.wait()
            with (root/"restarts.jsonl").open("a",encoding="utf8") as file:
                file.write(json.dumps(dict(run_id=run_id,exit_code=result,observed_at=datetime.now(CN).isoformat()))+"\n")
            time.sleep(5)
    finally:
        guard.unlink()


if __name__=="__main__":
    main()
