"""Real task progress, never a background liveness heartbeat."""
from datetime import datetime, timezone
import os
import time
from .paper_broker import atomic


class TaskProgress:
    def __init__(self, path, policy):
        self.path = path
        self.body = dict(pid=os.getpid(), policy=policy, trade_api_called=False,
                         sequence=0, stage='STARTING', last_completed_at=None, stage_failures={})

    def write(self):
        self.body['at'] = datetime.now(timezone.utc).isoformat()
        atomic(self.path, self.body)

    def run(self, stage, callback):
        self.body.update(stage=stage, stage_started_at=datetime.now(timezone.utc).isoformat(),
                         status='RUNNING', diagnostic=None)
        self.write()
        began = time.monotonic()
        try:
            value = callback()
        except Exception as exc:
            self.body.update(status='FAILED', diagnostic=getattr(exc, 'details', None)
                             or dict(category=type(exc).__name__))
            self.body['stage_failures'][stage]=self.body['diagnostic']
            raise
        else:
            self.body['stage_failures'].pop(stage,None)
            self.body.update(status='COMPLETED', last_completed_at=datetime.now(timezone.utc).isoformat(),
                             sequence=self.body['sequence']+1)
            return value
        finally:
            self.body['duration_seconds'] = time.monotonic()-began
            self.write()


def progress_problem(body, pid, now):
    if body['pid'] != pid:
        return 'TREND_NOTIFIER_HEALTH_STALE'
    at = datetime.fromisoformat(body['at'])
    if at.tzinfo is None or not 0 <= (now-at).total_seconds() <= 30:
        return 'TREND_NOTIFIER_HEALTH_STALE'
    if body.get('status') == 'FAILED':
        return 'TREND_NOTIFIER_STAGE_FAILED:'+str(body.get('stage'))
    if body.get('stage_failures'):
        return 'TREND_NOTIFIER_STAGE_FAILED:'+sorted(body['stage_failures'])[0]
    # An unrelated completed stage cannot conceal an unfinished slow stage.
    started = body.get('stage_started_at')
    if body.get('status') == 'RUNNING' and started:
        age = (now-datetime.fromisoformat(started)).total_seconds()
        if not 0 <= age <= 30:
            return 'TREND_NOTIFIER_PROGRESS_STALE:'+str(body.get('stage'))
    return None
