"""Small SDK-independent structural proof guard, also imported by the native bridge."""
from datetime import datetime
import math

PROOF_FIELDS=('anchor','anchor_bar_id','anchor_confirmed_at','anchor_episode_id','trend_episode_id','anchor_validated','signal_at')
EXECUTION_FIELDS=('evidence_created_at','first_eligible_at','expires_at','decision_at','entry_permission','permission_policy','permission_eob','permission_direction','reference_opponent_price')


def validate_execution_proof(row, now, side=None):
    from datetime import timedelta
    if row.get('entry_permission') is not True or row.get('permission_policy')!='CLOSED_FACTS_PERMISSION_V1':
        raise ValueError('SHARED_ENTRY_PERMISSION_REQUIRED')
    if side is not None and row.get('permission_direction')!=('bull' if side==1 else 'bear'):
        raise ValueError('ENTRY_DIRECTION_PERMISSION_MISMATCH')
    if side is not None:
        reference=float(row['reference_opponent_price']);price=float(row.get('price',reference))
        if not math.isfinite(reference) or reference<=0 or (price>reference+1e-8 if side==1 else price<reference-1e-8):
            raise ValueError('PRICE_MOVED_BEYOND_ORIGINAL_OPPONENT')
    def clock(key):
        t=datetime.fromisoformat(str(row[key]).replace('Z','+00:00'))
        if t.tzinfo is None:raise ValueError('UNKNOWN_EVIDENCE_CLOCK')
        return t
    source, first, expiry, decision, permission = [clock(k) for k in
        ('evidence_created_at','first_eligible_at','expires_at','decision_at','permission_eob')]
    if not source<=first<=decision<=now<=expiry or expiry!=first+timedelta(seconds=30) or permission!=first:
        raise ValueError('EXPIRED_OR_INCONSISTENT_EVIDENCE_CLOCK')
    if clock('signal_at')!=first:raise ValueError('EVIDENCE_CLOCK_RENEWED')

def validate_anchor_proof(row,symbol,side,entry):
    if row.get('anchor_validated') is not True:raise ValueError('INVALID_ANCHOR_PROOF')
    if not row.get('anchor_episode_id') or row.get('anchor_episode_id')!=row.get('trend_episode_id'):raise ValueError('INVALID_ANCHOR_EPISODE')
    bar_id=row.get('anchor_bar_id','')
    if not bar_id.startswith(symbol+'|900s|'):raise ValueError('INVALID_ANCHOR_IDENTITY')
    def time(v):
        t=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        if t.tzinfo is None:raise ValueError('UNKNOWN_ANCHOR_CLOCK')
        return t
    source=time(bar_id.rsplit('|',1)[-1]);confirmed=time(row['anchor_confirmed_at']);decision=time(row['signal_at'])
    if not source<=confirmed<=decision:raise ValueError('INVALID_ANCHOR_CONFIRMATION_CLOCK')
    anchor=float(row['anchor'])
    if not math.isfinite(anchor) or anchor<=0 or (anchor>=entry if side==1 else anchor<=entry):raise ValueError('INVALID_ANCHOR_SIDE')
    return anchor
