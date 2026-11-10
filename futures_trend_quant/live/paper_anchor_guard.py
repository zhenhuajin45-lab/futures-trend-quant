"""Small SDK-independent structural proof guard, also imported by the native bridge."""
from datetime import datetime
import math

PROOF_FIELDS=('anchor','anchor_bar_id','anchor_confirmed_at','anchor_episode_id','trend_episode_id','anchor_validated','signal_at')
EXECUTION_FIELDS=('evidence_created_at','first_eligible_at','expires_at','decision_at','entry_permission','permission_policy','permission_eob','permission_direction','reference_opponent_price','entry_policy_proof')


def validate_execution_proof(row, now, side=None):
    from datetime import timedelta
    if row.get('entry_permission') is not True or row.get('permission_policy') not in ('CLOSED_FACTS_PERMISSION_V1','CLOSED_FACTS_PERMISSION_V2_20261111','PAPER_ENTRY_V5'):
        raise ValueError('SHARED_ENTRY_PERMISSION_REQUIRED')
    if row.get('permission_policy')=='PAPER_ENTRY_V5':validate_v5_path_proof(row,side)
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


def validate_v5_path_proof(row,side):
    if side not in (1,2):raise ValueError('V5_EXECUTION_SIDE_REQUIRED')
    p=row.get('entry_policy_proof') or {};direction='bull' if side==1 else 'bear'
    path=row.get('path');evidence=row.get('core_evidence_id')
    if p.get('policy')!='PAPER_ENTRY_V5' or p.get('eob')!=row.get('permission_eob') or p.get('path')!=path or path not in ('A','B','C','D') or p.get('evidence_id')!=evidence or not evidence or p.get('allowed') is not True or p.get('reasons')!=[]:
        raise ValueError('V5_PATH_PROOF_IDENTITY')
    hist=p.get('hist')
    if type(hist) not in (int,float) or not math.isfinite(hist):raise ValueError('V5_MACD_UNKNOWN')
    if p.get('close_side')!=('above' if side==1 else 'below') or p.get('slope')!=('up' if side==1 else 'down') or p.get('opposite_divergence') is not False or p.get('closed_h1_known') is not True or p.get('calendar_qualified') is not True:
        raise ValueError('V5_PATH_FACTS_REJECTED')
    if path!='B' and not (hist>0 if side==1 else hist<0):raise ValueError('V5_MACD_HARD_GATE')
    h=p.get('h1_source') or {}
    if p.get('h1_absolute') not in ('bull_strong','bull_weakening','flat','bear_strong','bear_weakening'):raise ValueError('V5_H1_ABSOLUTE_UNKNOWN')
    if h.get('closed') is not True or h.get('calendar_qualified') is not True or datetime.fromisoformat(h['entry_eob'])>datetime.fromisoformat(row['permission_eob']):raise ValueError('V5_H1_PROOF')
    if path=='B':
        if p.get('m5_reverse_divergence') is not False or (p.get('h1_absolute')==('bear_strong' if side==1 else 'bull_strong') and not p.get('b_h1_exception')):raise ValueError('V5_B_ORIGINAL_H1_M5_VETO')
        b=p.get('b_process') or {};symbol=row['symbol'];start=b.get('start_bar_id','');confirm=b.get('confirmation_bar_id','')
        if not start.startswith(symbol+'|300s|') or not confirm.startswith(symbol+'|300s|') or b.get('status')!='CONFIRMED_ACTIVE' or not b.get('session_id') or not 0<=int(b.get('age',99))<6:
            raise ValueError('V5_B_PROCESS_INVALID')
        if not datetime.fromisoformat(b['start_eob'])<datetime.fromisoformat(b['confirmation_eob'])<=datetime.fromisoformat(b['last_m5_eob'])<=datetime.fromisoformat(row['permission_eob']):raise ValueError('V5_B_PROCESS_CLOCK')
        if (datetime.fromisoformat(b['last_m5_eob'])-datetime.fromisoformat(b['start_eob'])).total_seconds()!=int(b['age'])*300:raise ValueError('V5_B_PROCESS_AGE')
        if start.rsplit('|',1)[-1]!=b['start_eob'] or confirm.rsplit('|',1)[-1]!=b['confirmation_eob']:raise ValueError('V5_B_BAR_CLOCK')
        expected=f"B|{direction}|{b['process_id']}|{confirm}"
        if evidence!=expected or b['process_id']!=symbol+'|'+direction+'|'+start:raise ValueError('V5_B_PROCESS_IDENTITY')

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
