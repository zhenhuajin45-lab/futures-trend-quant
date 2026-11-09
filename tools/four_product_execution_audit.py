"""Freeze existing local evidence and audit the four completed futures sessions.

No SDK, process control, account mutation or historical data fetch. Raw prefixes
stay in the private directory; only derived rows may be reviewed for publication.
"""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import argparse, csv, hashlib, json

PRODUCTS={'DCE.M','CZCE.OI','DCE.Y','DCE.P'}


def audit(client, private, output, reuse=False):
    ev=client/'_strategy_runtime/evidence/paper_observation_five'
    private.mkdir(parents=True,exist_ok=reuse);output.mkdir(parents=True,exist_ok=True)
    index=[];sources={}
    for name in ('signals.jsonl','order_events.jsonl','runtime_events.jsonl',
                 'rpc_trace.local.jsonl','rpc_trace_reader.local.jsonl','status.json',
                 'service_health.local.json','clock_health.local.json','run_manifest.json'):
        p=private/name if reuse else ev/name
        with p.open('rb') as f:blob=f.read(p.stat().st_size)
        if not reuse:(private/name).write_bytes(blob)
        index.append(dict(name=name,prefix_bytes=len(blob),sha256=hashlib.sha256(blob).hexdigest()))
        sources[name]=[json.loads(x) for x in blob.splitlines()] if name.endswith('jsonl') else json.loads(blob)
    state=private/'execution.local.json' if reuse else client/'_strategy_runtime/state/paper_observation_five/execution.json'
    blob=state.read_bytes()
    if not reuse:(private/'execution.local.json').write_bytes(blob)
    index.append(dict(name='execution.local.json',prefix_bytes=len(blob),sha256=hashlib.sha256(blob).hexdigest()))
    state=json.loads(blob);intents=state['intents'];orders=sources['order_events.jsonl'];rows=[];candidates=[]
    cutoff=datetime.fromisoformat('2026-11-09T23:00:00+08:00')
    start=datetime.fromisoformat('2026-11-09T00:00:00+08:00')
    for s in sources['signals.jsonl']:
        bar=datetime.fromisoformat(s['bar_eob']);cs=s['csymbol']
        if cs not in PRODUCTS or not start<=bar<=cutoff:continue
        entry=s.get('entry') or {};facts=s.get('facts') or {};obs=s.get('trend_observation') or {}
        matches=[i for i in intents.values() if i.get('csymbol')==cs and i.get('signal_at')==s['bar_eob']]
        first=entry.get('reason') or 'NO_DIRECTION_AUTHORIZATION'
        terminal=state.get('candidate_status',{}).get(cs+'|'+str(entry.get('evidence_id')), {})
        if entry.get('accepted'):
            first=matches[0].get('local_reject_reason') if matches else terminal.get('reason','UNKNOWN_NO_TERMINAL_RECORD')
        rows.append(dict(product=cs,bar_eob=s['bar_eob'],evaluated_at=s.get('order_evaluated_at'),
            path=entry.get('path'),technical_accepted=bool(entry.get('accepted')),
            path_rejections=json.dumps({a['path']:a.get('reasons',[]) for a in (obs.get('candidate_audit') or facts.get('candidate_audit') or [])},ensure_ascii=False),
            first_outcome=first,local_intents=len(matches),native_invocations=sum(i.get('native_submit_invoked') is True for i in matches),
            broker_orders=sum(bool(i.get('order_id')) for i in matches),filled=sum(i.get('filled',0) for i in matches),
            native_after_rejection='NOT_EVALUATED' if matches and matches[0].get('native_submit_invoked') is False else 'UNKNOWN'))
        if entry.get('accepted'):
            candidates.append(dict(product=cs,bar_eob=s['bar_eob'],evaluated_at=s.get('order_evaluated_at'),
                entry=entry,terminal=terminal,trend_observation=obs,facts=facts,
                intents=[{k:i.get(k) for k in ('id','symbol','path','price','stop','target','status','filled','order_id',
                    'native_submit_invoked','execution_stage','local_reject_reason','submitted_at','expires_at','decision_quote','entry_policy_proof')} for i in matches],
                order_timeline=[o for o in orders if o.get('rpc_id') in {i['id'] for i in matches}],
                execution_price_supported='NO_CABINET_SUBMIT_OR_FILL_EVIDENCE'))
    def write(name,data):(output/name).write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf8')
    with (output/'funnel.csv').open('w',encoding='utf8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    summary={cs:dict(evaluations=sum(x['product']==cs for x in rows),
        accepted=sum(x['technical_accepted'] for x in rows if x['product']==cs),
        first_outcomes=dict(Counter(x['first_outcome'] for x in rows if x['product']==cs)),
        local_intents=sum(x['local_intents'] for x in rows if x['product']==cs),
        native_invocations=sum(x['native_invocations'] for x in rows if x['product']==cs),
        broker_orders=sum(x['broker_orders'] for x in rows if x['product']==cs),
        filled=sum(x['filled'] for x in rows if x['product']==cs)) for cs in sorted(PRODUCTS)}
    write('summary.json',dict(frozen_at=datetime.now(timezone.utc).isoformat(),bar_cutoff=cutoff.isoformat(),
        scope='FOUR_PRODUCTS_DAY_AND_COMPLETED_NIGHT;GOLD_NOT_FINAL',products=summary,
        warning='Derived evidence, no hypothetical fills or profitability claims; unknown native subconditions stay UNKNOWN.'))
    write('candidates.local.json',candidates);write('prefix_index.json',index)
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--client',type=Path,required=True);p.add_argument('--private',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--reuse-frozen',action='store_true');a=p.parse_args()
    print(json.dumps(audit(a.client,a.private,a.output,a.reuse_frozen),ensure_ascii=False))
