"""Bounded parallel source probes; slow DNS cannot hold the publisher loop.

Exactly one daemon worker per source. Late replies are discarded by cycle ID,
never retimed or combined with a newer collection. No OS clock mutation.
"""
from datetime import datetime, timezone
import queue
import threading
import time

POLICY = 'CLOCK_PARALLEL_BOUNDED_V1_20261114'


class BoundedClockCollector:
    def __init__(self, sampler=None, budget_seconds=8.):
        from .paper_clock_health import probe, SOURCES
        self.sampler = sampler or probe
        self.sources = SOURCES
        self.budget = budget_seconds
        if not 0 < self.budget <= 8.:
            raise ValueError('CLOCK_PROBE_BUDGET')
        self.sequence = 0
        self.pending = {}
        self.tasks = {s: queue.Queue(maxsize=1) for s in self.sources}
        self.results = queue.Queue(maxsize=len(self.sources))
        for source in self.sources:
            threading.Thread(target=self._worker, args=(source,), daemon=True,
                             name='bounded-ntp-' + source).start()

    def _worker(self, source):
        while True:
            cycle, attempt = self.tasks[source].get()
            started = datetime.now(timezone.utc).isoformat()
            mono = time.monotonic()
            try:
                result, error = self.sampler(source), None
            except Exception as exc:
                result = None
                error = dict(source=source, error=type(exc).__name__, attempt=attempt,
                             retryable=isinstance(exc, (TimeoutError, OSError)),
                             stage=getattr(exc, 'probe_stage', 'SOURCE_PROBE'),
                             errno=getattr(exc, 'errno', None), winerror=getattr(exc, 'winerror', None),
                             started_at=started, duration_seconds=time.monotonic()-mono)
            self.results.put((source, cycle, result, error))

    def collect(self, config_sha256, run_id):
        from .paper_clock_health import measurement
        started = datetime.now(timezone.utc).isoformat()
        mono = time.monotonic()
        self.sequence += 1
        cycle = self.sequence
        samples, errors, attempts, finished = {}, [], dict.fromkeys(self.sources, 0), set()
        while time.monotonic()-mono < self.budget:
            # A previous blocked DNS call retains its sole slot. Do not spawn
            # replacement threads or queue unlimited work behind it.
            for source in self.sources:
                if source not in finished and source not in self.pending and attempts[source] < 3:
                    attempts[source] += 1
                    self.pending[source] = cycle
                    self.tasks[source].put_nowait((cycle, attempts[source]))
            try:
                source, reply_cycle, sample, error = self.results.get(
                    timeout=min(.05, max(.0001, self.budget-(time.monotonic()-mono))))
            except queue.Empty:
                continue
            if self.pending.get(source) == reply_cycle:
                del self.pending[source]
            if reply_cycle != cycle:
                continue  # Late completion has no availability in this cycle.
            if error is None:
                samples[source] = sample
                finished.add(source)
            else:
                errors.append(error)
                if not error['retryable'] or attempts[source] >= 3:
                    finished.add(source)
            if len(finished) == len(self.sources):
                break
        for source in self.sources:
            if source not in finished:
                errors.append(dict(source=source, error='TimeoutError', stage='COLLECTION_DEADLINE',
                                   attempt=attempts[source], retryable=True,
                                   started_at=started, duration_seconds=time.monotonic()-mono,
                                   pending_cycle=self.pending.get(source)))
        row = measurement(config_sha256, run_id, started, mono,
                          [samples[s] for s in self.sources if s in samples], errors)
        return dict(row, probe_policy=POLICY, probe_budget_seconds=self.budget,
                    probe_cycle=cycle, pending_sources=sorted(self.pending))
