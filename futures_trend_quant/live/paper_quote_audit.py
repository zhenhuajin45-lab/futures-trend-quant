"""Bounded quote handoff evidence. Never selects or reprices a quote."""
from datetime import datetime,timezone
import json,time

POLICY='QUOTE_HANDOFF_EVIDENCE_V1_20261113'


def record(r,symbol,incoming,received,previous,accepted):
    now=time.monotonic();state=getattr(r,'quote_audit_state',None)
    if state is None:state={};r.quote_audit_state=state
    key=(symbol,incoming.get('source'),bool(accepted));prior=state.get(key)
    switched=bool(accepted and previous and previous.get('source')!=incoming.get('source'))
    switch_key=(symbol,'SOURCE_SWITCH')
    switch_due=switched and now-state.get(switch_key,float('-inf'))>=1
    if prior and not switch_due and now-prior['mono']<5:
        prior['suppressed']+=1;return
    # Original source/receive clocks and book, including rejected callbacks.
    row=dict(policy=POLICY,host_run_id=getattr(r,'run_id',None),config_sha256=r.c.get('config_sha256'),
        symbol=symbol,checked_at=datetime.now(timezone.utc).isoformat(),received_at=str(received),
        outcome='ACCEPTED' if accepted else 'OLDER_OR_EQUAL_LOWER_PRIORITY',
        source_switch=switched,incoming=dict(incoming),previous=None if previous is None else dict(previous),
        suppressed_same_outcome=0 if prior is None else prior['suppressed'])
    state[key]=dict(mono=now,suppressed=0)
    if switch_due:state[switch_key]=now
    with (r.evidence/'quote_handoff.local.jsonl').open('a',encoding='utf8') as f:
        f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
