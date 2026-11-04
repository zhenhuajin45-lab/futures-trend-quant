"""Research-only as-of filter. This module cannot inspect prices after a signal."""
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite


@dataclass(frozen=True)
class Evidence:
    opportunity_id: str
    evidence_id: str
    accepted: bool
    structural_stop_valid: bool


@dataclass(frozen=True)
class Frame:
    csymbol: str
    actual_contract: str
    eob: str
    bar_sequence: int
    direction: str
    evidence: tuple[Evidence, ...] = ()
    selected_contract: bool = True
    in_scoring_window: bool = True
    contiguous: bool = True
    # Only a verified preceding state (including none) can establish a first segment.
    previous_state_verified: bool = False
    previous_direction: str | None = None
    v6_episode: int | None = None


@dataclass
class Leg:
    direction: str = 'none'
    last_eob: str | None = None
    last_sequence: int | None = None
    episode_id: str | None = None
    start_known: bool = False
    slot: str | None = None
    seen: set = field(default_factory=set)


class FirstValidOnly:
    """Consume at signal time; roll legs have independent, retained state."""
    def __init__(self):
        self.legs = {}
        self.episodes = {}

    def step(self, frame: Frame):
        if frame.direction not in ('bull', 'bear', 'none'):
            raise ValueError('unknown direction')
        time = datetime.fromisoformat(frame.eob)
        if time.tzinfo is None:
            raise ValueError('timezone required')
        key = (frame.csymbol, frame.actual_contract)
        leg = self.legs.setdefault(key, Leg())
        if leg.last_eob is not None and (time <= datetime.fromisoformat(leg.last_eob) or
                                         frame.bar_sequence <= leg.last_sequence):
            raise ValueError('non-increasing closed frame')
        prior = leg.direction if leg.last_eob else frame.previous_direction
        prior_known = leg.last_eob is not None or frame.previous_state_verified
        interrupted = not frame.contiguous
        changed = prior != frame.direction
        if changed or interrupted or leg.last_eob is None:
            if leg.episode_id is not None:
                self.episodes[leg.episode_id]['end_known'] = not interrupted
                self.episodes[leg.episode_id]['end_eob'] = frame.eob if not interrupted else None
            leg.slot = None
            leg.start_known = bool(prior_known and changed and not interrupted)
            leg.episode_id = None
            if frame.direction != 'none':
                marker = '' if leg.start_known else 'LEFT_CENSORED@'
                leg.episode_id = '|'.join((*key, frame.direction, marker+frame.eob))
                self.episodes[leg.episode_id] = dict(csymbol=frame.csymbol, actual_contract=frame.actual_contract,
                    direction=frame.direction, start_eob=frame.eob, start_known=leg.start_known,
                    end_known=False, end_eob=None, v6_episode=frame.v6_episode, selected_key=None)
        leg.direction = frame.direction
        leg.last_eob, leg.last_sequence = frame.eob, frame.bar_sequence
        results = []
        for evidence in sorted(frame.evidence, key=lambda e: e.opportunity_id):
            if not evidence.opportunity_id or not evidence.evidence_id:
                reason = 'MISSING_ID'
            elif not frame.selected_contract:
                reason = 'NOT_SELECTED_ACTUAL_CONTRACT'
            elif frame.direction == 'none':
                reason = 'NO_DIRECTION'
            elif not evidence.accepted or not evidence.structural_stop_valid:
                reason = 'NOT_VALID_AT_SIGNAL'
            elif evidence.opportunity_id in leg.seen:
                reason = 'DUPLICATE_EVIDENCE'
            else:
                # V6 evidence keys include direction/path and survive episode changes.
                leg.seen.add(evidence.opportunity_id)
                if not leg.start_known:
                    reason = 'LEFT_CENSORED_FIRST_UNKNOWN'
                elif leg.slot is not None:
                    reason = 'EPISODE_SLOT_ALREADY_CONSUMED'
                else:
                    leg.slot = evidence.opportunity_id
                    self.episodes[leg.episode_id]['selected_key'] = leg.slot
                    reason = 'KEEP' if frame.in_scoring_window else 'PRE_SAMPLE_CONSUMED'
            results.append(dict(opportunity_id=evidence.opportunity_id, episode_id=leg.episode_id,
                eob=frame.eob, kept=reason == 'KEEP', reason=reason, slot=leg.slot,
                episode_start_known=leg.start_known, v6_episode=frame.v6_episode))
        return results


def frame_from_v6(task, row, index, tick, rules, *, in_scoring_window,
                  contiguous=True, previous_state_verified=False, previous_direction=None, v6_episode=None):
    """Same closed-frame/stop validation as V6 extract, before label_opportunity.

    Explicit allowlist: labels, next open, future completeness and PnL are never read.
    The caller must feed ALL closed frames (also unselected legs and prehistory).
    """
    from futures_trend_quant.core.risk import build_stop_plan
    core = row['groups']['S0']
    direction = core['trend_decision']['state']['trend_state']
    entry = core['entry_evaluation'] or {}
    evidence = ()
    if entry.get('accepted') and entry.get('evidence_id') and direction in ('bull', 'bear'):
        key = '|'.join((task['csymbol'], task['symbol'], direction, entry['path'], entry['evidence_id']))
        valid = False
        try:
            close, atr, anchor = float(core['close']), float(core['atr14']), float(entry['anchor']['anchor_price'])
            if not all(isfinite(x) for x in (close, atr, anchor, tick)) or atr <= 0 or tick <= 0:
                raise ValueError('invalid signal-time geometry')
            build_stop_plan(direction=direction, entry=close, anchor=anchor, atr=atr, tick=tick,
                buffer_atr=rules['stop_buffer_atr'], min_atr=rules['stop_min_atr'],
                max_atr=rules['stop_max_atr'], reward_r=2)
            valid = True
        except (ValueError, TypeError, KeyError):
            pass
        evidence = (Evidence(key, entry['evidence_id'], True, valid),)
    return Frame(task['csymbol'], task['symbol'], str(row['decision_eob']), index, direction, evidence,
        selected_contract=row['selected'], in_scoring_window=in_scoring_window,
        contiguous=contiguous and bool(row['complete_m15']), previous_state_verified=previous_state_verified,
        previous_direction=previous_direction, v6_episode=v6_episode)


def filter_frames(frames):
    selector = FirstValidOnly()
    decisions = []
    # Sorting only uses registered, closed-time fields. Stable across input storage order.
    for frame in sorted(frames, key=lambda x: (datetime.fromisoformat(x.eob), x.csymbol,
                                              x.actual_contract, x.bar_sequence)):
        decisions.extend(selector.step(frame))
    return decisions, list(selector.episodes.values())
