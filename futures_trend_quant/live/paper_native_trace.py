"""Native SDK stage timing only. Never log credentials, arguments or results."""
from datetime import datetime,timezone
from pathlib import Path
import json,time


class NativeTrace:
    def __init__(self,root,config,role):
        self.path=Path(root)/config.get('evidence_dir','evidence/paper_native')/('native_stages_reader.local.jsonl' if role=='READ_ONLY' else 'native_stages.local.jsonl')
        self.path.parent.mkdir(parents=True,exist_ok=True);self.role=role

    def call(self,request_id,op,stage,fn):
        started=time.monotonic()
        def emit(phase,**extra):
            with self.path.open('a',encoding='utf8') as f:
                f.write(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(),rpc_id=request_id,
                    op=op,role=self.role,stage=stage,phase=phase,**extra))+'\n');f.flush()
        emit('START')
        try:result=fn()
        except Exception as exc:
            emit('FAILED',elapsed_seconds=time.monotonic()-started,error_type=type(exc).__name__)
            raise
        emit('FINISH',elapsed_seconds=time.monotonic()-started)
        return result
