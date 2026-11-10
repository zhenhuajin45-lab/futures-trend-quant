"""Own state only. An observer cannot open or mutate the execution ledger."""
from pathlib import Path
import json
import os
import tempfile
from .models import POLICY, SCHEMA


def atomic(path, value):
    from futures_trend_quant.live.paper_io import native_path
    path=Path(native_path(path));path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.pa_',suffix='.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(value,f,ensure_ascii=False,sort_keys=True,allow_nan=False,default=str)
            f.write('\n');f.flush();os.fsync(f.fileno())
        from futures_trend_quant.live.paper_io import replace_file
        replace_file(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


class State:
    def __init__(self, directory, parameter_hash):
        self.path=Path(directory)/'observer.json'
        if self.path.exists():
            from futures_trend_quant.live.paper_io import read_json
            self.data=read_json(self.path)
            if (self.data.get('schema_version')==SCHEMA and self.data.get('policy')=='PRICE_ACTION_SHADOW_V1_20261110'
                    and self.data.get('parameters_sha256')==parameter_hash):
                from datetime import datetime, timezone
                from .models import digest
                previous=digest(self.data)
                self.data.setdefault('migrations',[]).append(dict(
                    source_policy=self.data['policy'],target_policy=POLICY,source_state_sha256=previous,
                    migrated_at=datetime.now(timezone.utc).isoformat(),kind='ADDITIVE_CONSUMER_WINDOW_COVERAGE',
                    persisted_known_at_preserved=True,original_events_retained=True))
                self.data['policy']=POLICY
            if self.data.get('policy')!=POLICY or self.data.get('parameters_sha256')!=parameter_hash:
                raise ValueError('PA_STATE_IDENTITY_REQUIRES_EXPLICIT_MIGRATION')
        else:
            self.data=dict(schema_version=SCHEMA,policy=POLICY,parameters_sha256=parameter_hash,
                           products={},emitted={},gaps=[],trade_api_called=False)

    def save(self):
        atomic(self.path,self.data)
