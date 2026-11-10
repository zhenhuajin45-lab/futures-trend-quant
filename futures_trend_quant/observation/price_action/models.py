from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math

POLICY = 'PRICE_ACTION_SHADOW_V1_20261110'
SCHEMA = 1
DEFAULTS = dict(mode='SHADOW', pivot_k=2, leg_min_atr=1.2, zone_ticks=2,
                zone_atr=.1, range_window=20, range_touches=2,
                middle_low=.35, middle_high=.65, acceptance_ticks=1,
                short_box_bars=4, short_box_max_atr=1.5, reference_rr=2,
                queue_size=128, min_days=20, min_candidates=200, min_products=3)


def clock(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError('PA_TIMEZONE_UNKNOWN')
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, default=str).encode('utf8')).hexdigest()


def parameters(raw=None):
    result = dict(DEFAULTS, **(raw or {}))
    if set(result) != set(DEFAULTS) or result['mode'] != 'SHADOW':
        raise ValueError('PA_SHADOW_ONLY_CONFIG')
    # Research values are preregistered; changing them requires a new experiment.
    if result != DEFAULTS:
        raise ValueError('PA_UNREGISTERED_PARAMETER_CHANGE')
    return result


@dataclass(frozen=True)
class PivotFact:
    event_id: str
    bar_id: str
    kind: str
    price: float
    occurred_at: str
    confirmed_at: str
    known_at: str | None
    atr: float

    def row(self):
        return asdict(self)


@dataclass(frozen=True)
class Zone:
    zone_id: str
    kind: str
    role: str
    lower: float
    upper: float
    source_ids: tuple
    born_at: str
    known_at: str | None

    def __post_init__(self):
        if not all(math.isfinite(x) for x in (self.lower, self.upper)) or self.lower > self.upper:
            raise ValueError('PA_INVALID_ZONE')

    def row(self):
        return asdict(self)
