"""Research-only termination proof for an OWNED read-only worker, not a writer.

The caller supplies ownership and readonly role proof. No spawning, account login,
replacement writer or automatic recovery is implemented here.
"""
def stop_owned_read_worker(process, *, owned_pid, role, lease, at, wait_seconds=2):
    if role!='READ_ONLY' or process.pid!=owned_pid:
        raise ValueError('only explicitly owned read-only worker may be stopped')
    if lease.current is None or lease.current['op']!='snapshot':
        raise ValueError('submit/cancel or unknown operation must not use read-worker recovery')
    lease.stop_requested(at)
    if process.poll() is None:process.terminate()
    # A caller timeout is NOT proof. Only process wait/poll confirms local death.
    code=process.wait(timeout=wait_seconds)
    if process.poll() is None:raise RuntimeError('worker still alive; lease retained')
    lease.child_exit_proven(at)
    return dict(owned_pid=owned_pid,worker_exit_code=code,local_worker_dead=True,
                broker_order_completion=False,writer_restart_allowed=False,
                fresh_reconciliation_required=True)
