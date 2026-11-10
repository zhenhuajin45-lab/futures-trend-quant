"""Read-only, two-source UTC checks. Never correct a clock or retime market data.

V1 engineering bounds reserve at most 0.5s of the existing 3s quote budget for
clock error. Both sources must agree within 0.1s; samples expire after 90s of
system monotonic time. A failed probe never refreshes an old successful sample.
This module is stdlib-only: the native Python 3.10 writer uses the same guard.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import os
import socket
import statistics
import struct
import tempfile
import time

VERSION = 'PAPER_CLOCK_HEALTH_V1_20261109'
SOURCE_POLICY = 'ALIYUN_TENCENT_PUBLIC_NTP_V1_20261109'
SOURCES = ('ntp.aliyun.com', 'ntp.tencent.com')
MAX_OFFSET = .5
MAX_DISAGREEMENT = .1
MAX_RTT = 1.
MAX_AGE = 90.


def atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.clock_')
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as f:
            json.dump(value, f, ensure_ascii=False,allow_nan=False); f.flush(); os.fsync(f.fileno())
        for attempt in range(5):
            try:
                from .paper_io import replace_file
                replace_file(tmp,path);break
            except PermissionError as exc:
                if getattr(exc,'winerror',None) not in (5,32,33) or attempt==4:raise
                time.sleep(.01*(attempt+1))
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def probe(source):
    # Resolve before recording t1; DNS time is not NTP transit time.
    address = socket.gethostbyname(source)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(2.)
        packet = bytearray(48); packet[0] = 0x23
        wall = time.time(); mono = time.monotonic(); nt = wall + 2208988800
        sent = (int(nt), int((nt - int(nt)) * 2**32))
        struct.pack_into('!II', packet, 40, *sent)
        s.sendto(packet, (address, 123)); reply, peer = s.recvfrom(512)
        done = time.time(); elapsed = time.monotonic() - mono
    if len(reply) < 48 or peer[0] != address: raise ValueError('NTP_PACKET_IDENTITY')
    values = struct.unpack('!12I', reply[:48])
    if (values[6], values[7]) != sent or reply[0] >> 6 == 3 or reply[0] & 7 != 4 or not 1 <= reply[1] <= 15:
        raise ValueError('NTP_PACKET_INVALID')
    if not 0 <= elapsed <= MAX_RTT or abs(done - wall - elapsed) > .1:
        raise ValueError('NTP_RTT_OR_CLOCK_STEP')
    received = values[8] - 2208988800 + values[9] / 2**32
    transmitted = values[10] - 2208988800 + values[11] / 2**32
    return dict(source=source, offset_seconds=((received-wall)+(transmitted-done))/2,
                rtt_seconds=elapsed, stratum=reply[1])


def collect(config_sha256, run_id, sampler=None):
    sampler = sampler or probe; samples = []; errors = []
    # Age from the beginning of collection, never give earlier replies a new
    # lifetime after waiting for another source. Retries stay in this child.
    started=datetime.now(timezone.utc).isoformat();started_mono=time.monotonic()
    for source in SOURCES:
        for attempt in range(3):
            try:
                samples.append(sampler(source));break
            except Exception as exc:
                retryable=isinstance(exc,(TimeoutError,OSError))
                errors.append(dict(source=source,error=type(exc).__name__,attempt=attempt+1,retryable=retryable))
                if not retryable:break
    offsets = [s.get('offset_seconds') for s in samples]
    valid = (len(samples) == 2 and {s.get('source') for s in samples} == set(SOURCES)
             and all(type(x) in (int, float) and math.isfinite(x) for x in offsets))
    consensus = valid and max(offsets)-min(offsets) <= MAX_DISAGREEMENT
    healthy = consensus and all(abs(x) <= MAX_OFFSET for x in offsets)
    reason=('OK' if healthy else 'CLOCK_OFFSET' if valid and any(abs(x)>MAX_OFFSET for x in offsets)
            else 'CLOCK_SOURCE_DISAGREEMENT' if valid else 'CLOCK_SOURCE_QUORUM')
    return dict(policy=VERSION, source_policy=SOURCE_POLICY, at=started,
                sample_monotonic=started_mono, config_sha256=config_sha256, run_id=run_id,
                status='HEALTHY' if healthy else 'UNTRUSTED',
                reason=reason,
                measured_offset_seconds=statistics.median(offsets) if valid else None,
                samples=samples, errors=errors, max_offset_seconds=MAX_OFFSET,
                max_sample_age_seconds=MAX_AGE, clock_changed=False)


def clock_process(evidence_dir, config_sha256, run_id):
    path = Path(evidence_dir) / 'clock_health.local.json'
    previous=None
    while True:
        measurement=collect(config_sha256, run_id)
        row=retain_bounded_sample(previous,measurement)
        atomic(path,row)
        previous=row if row['status']=='HEALTHY' else None
        time.sleep(30)


def retain_bounded_sample(previous,measurement):
    """Packet loss may reuse a still-valid sample, never extend its deadline."""
    if (measurement['reason']=='CLOCK_SOURCE_QUORUM' and previous
            and measurement.get('errors') and all(x.get('retryable') is True for x in measurement['errors'])
            and all(type(x.get('offset_seconds')) in (int,float) and math.isfinite(x['offset_seconds'])
                    and abs(x['offset_seconds'])<=MAX_OFFSET for x in measurement.get('samples',[]))
            and previous.get('status')=='HEALTHY'
            and previous.get('run_id')==measurement['run_id']
            and previous.get('config_sha256')==measurement['config_sha256']
            and previous.get('source_policy')==measurement.get('source_policy')==SOURCE_POLICY
            and 0<=measurement['sample_monotonic']-previous['sample_monotonic']<=MAX_AGE):
        return dict(previous,probe_degraded=True,last_attempt_at=measurement['at'],
                    last_attempt_errors=measurement['errors'])
    return measurement


def health(config, *, root=None, monotonic=None, wall=None):
    if config.get('clock_policy_version') != VERSION: return dict(status='NOT_ENABLED', reason=None)
    root = Path(root or config.get('root', '.'))
    ev = root / config['evidence_dir']
    try:
        from .paper_io import read_json
        row = read_json(ev/'clock_health.local.json')
        manifest = read_json(ev/'run_manifest.json')
        digest = config.get('config_sha256')
        if digest is None: raise ValueError('CLOCK_PROFILE_IDENTITY')
        if (row['policy'] != VERSION or row.get('source_policy')!=SOURCE_POLICY
                or row['config_sha256'] != digest or row['run_id'] != manifest['run_id']):
            raise ValueError('CLOCK_RUN_IDENTITY')
        age = (time.monotonic() if monotonic is None else monotonic) - float(row['sample_monotonic'])
        if not math.isfinite(age) or not 0 <= age <= MAX_AGE: raise ValueError('CLOCK_HEALTH_STALE')
        sample_time=datetime.fromisoformat(row['at'])
        if sample_time.tzinfo is None:raise ValueError('CLOCK_TIMEZONE_UNKNOWN')
        sample_wall=sample_time.timestamp()
        wall_age=(time.time() if wall is None else wall)-sample_wall
        if abs(wall_age-age)>.25:raise ValueError('CLOCK_CHANGED_SINCE_SAMPLE')
        offsets = [s['offset_seconds'] for s in row['samples']]
        if (row['status'] != 'HEALTHY' or len(offsets) != 2
                or {s['source'] for s in row['samples']} != set(SOURCES)
                or not all(type(x) in (int,float) and math.isfinite(x) and abs(x)<=MAX_OFFSET for x in offsets)
                or max(offsets)-min(offsets)>MAX_DISAGREEMENT):
            raise ValueError('CLOCK_HEALTH_UNTRUSTED')
        return dict(status='DEGRADED' if row.get('probe_degraded') else 'HEALTHY', reason=None, age_seconds=age,
                    measured_offset_seconds=row['measured_offset_seconds'], run_id=row['run_id'],source_policy=SOURCE_POLICY)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        diagnostic = getattr(exc, 'details', None) or dict(code=str(exc)[:80], category=type(exc).__name__)
        return dict(status='BLOCKED', reason='SYSTEM_CLOCK_UNTRUSTED', detail=diagnostic['code'],
                    diagnostic=diagnostic)


def require_clock(config, *, root=None):
    state = health(config, root=root)
    if state['reason']:
        exc = ValueError('SYSTEM_CLOCK_UNTRUSTED')
        exc.details = dict(proof_type='CLOCK', **state)
        raise exc
    return state
