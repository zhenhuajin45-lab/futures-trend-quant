"""Join per-product offline replays and audit original cases against full episodes."""
import csv,json,argparse
from datetime import datetime,timedelta
from pathlib import Path
from collections import Counter
from .replay import read,write,csvwrite
from futures_trend_quant.core.risk import build_stop_plan


def rows(path):
    with path.open(encoding='utf-8',newline='') as f:return list(csv.DictReader(f))


def run(root,out):
    out.mkdir(exist_ok=True)
    metadata_root=root/'evidence/night_complete_20261106_20261107_r1/inputs'
    symbols={cs:read(metadata_root/cs.replace('.','_')/'metadata.json')['symbol'] for cs in ['CZCE.OI','DCE.M','DCE.P','DCE.Y','SHFE.AU']}
    datasets={}
    for name in ['state_trace_control','state_trace_ordered','m5_process_timeline','candidate_deltas','baseline_reproduction']:
        combined=[]
        for cs in ['CZCE_OI','DCE_M','DCE_P','DCE_Y','SHFE_AU']:
            p=root/'evidence'/('ordered_b_parallel_'+cs)/(name+'.csv')
            if p.stat().st_size>2:combined.extend(rows(p))
        for r in combined:r['symbol']=symbols[r['csymbol']]
        datasets[name]=combined
        csvwrite(out,name+'.csv',combined)
    assert len(datasets['baseline_reproduction'])==54
    matches=sum(r['parity']=='True' for r in datasets['baseline_reproduction'])
    original=rows(root/'reports/entry_compatibility_20261107_corrected/event_relation_audit.csv')
    cases=[]
    for r in original:
        if r['selected_this_path']!='True':continue
        cs=r['csymbol'];pattern=json.loads(r['second']);cutoff=r['eob'];pt=pattern['eob'].replace(' ','T')
        h=[x for x in datasets['m5_process_timeline'] if x['csymbol']==cs and x['at']<=cutoff]
        hits=[]
        for x in h:
            active=json.loads(x['active']) if x.get('active') else None
            if active and active.get('confirmation') and active['confirmation']['eob']==pt:hits.append(active)
        endbook=read(root/'evidence/ordered_b_state_replay_private'/(cs.replace('.','_')+'_episode.json'))
        ends=[x for x in endbook['ended'] if x['ended_at']<=cutoff and x.get('confirmation') and x['confirmation']['eob']==pt]
        touch=[x['at'] for x in h if x['near']=='True']
        status='原件或合同不足_未知' if r['path']=='C' or not h or pt<h[0]['at'] else '确认后破坏或新过程' if ends else '合法前置回调_最近摘要覆盖' if hits else '缺少合法前置关系'
        cases.append(dict(csymbol=cs,eob=cutoff,path=r['path'],original_pattern_confirmed=pt,
                          all_touch_times_before_pattern=[t for t in touch if t<pt],same_bar_touch=pt in touch,
                          all_touch_times_after_pattern=[t for t in touch if t>pt],classification=status,
                          status_scope='NEW_ORDERED_B_RULE_ONLY_NOT_PROOF_OF_OLD_BASELINE_BUG',matched_episodes=hits[-1:],ended=ends,
                          mechanism_status='UNKNOWN' if '未知' in status else 'QUALIFIED' if status.startswith('合法') else 'DISQUALIFIED'))
    csvwrite(out,'full_case_timelines.csv',cases)
    old_matrix=rows(root/'reports/entry_compatibility_20261107_corrected/path_condition_matrix.csv')
    old_events={};new_events={}
    for r in old_matrix:
        if r['path']!='B' or r['status']!='RAW_PATH':continue
        pattern=json.loads(r['m5_evidence'])['m5_pattern'];key=(r['csymbol'],pattern['bar_id'])
        old_events.setdefault(key,[]).append(dict(eob=r['eob'],trigger=r['path_trigger_eob']))
    for r in datasets['state_trace_ordered']:
        e=json.loads(r['entry']) if r.get('entry') else None
        for item in (e or {}).get('candidates',[]):
            if item['path']!='B':continue
            key=(r['csymbol'],item['trigger_bar_id'].split('|CONFIRM|')[-1])
            new_events.setdefault(key,[]).append(dict(eob=r['eob'],trigger=item['trigger_bar_id']))
    event_deltas=[]
    for key in sorted(old_events.keys()|new_events.keys()):
        old=old_events.get(key,[]);new=new_events.get(key,[])
        classification='ADDED' if not old else 'REMOVED' if not new else 'EARLIER' if new[0]['eob']<old[0]['eob'] else 'DELAYED' if new[0]['eob']>old[0]['eob'] else 'UNCHANGED_FIRST_OBSERVATION'
        event_deltas.append(dict(csymbol=key[0],pattern_id=key[1],classification=classification,
            old_observations=len(old),ordered_observations=len(new),old_first=None if not old else old[0]['eob'],ordered_first=None if not new else new[0]['eob'],
            old_trigger_ids=len({r['trigger'] for r in old}),ordered_trigger_ids=len({r['trigger'] for r in new}),
            merged_repeated_trigger=len({r['trigger'] for r in old})>len({r['trigger'] for r in new}) if new else False))
    csvwrite(out,'event_deltas.csv',event_deltas)
    inputs=root/'evidence/night_complete_20261106_20261107_r1/inputs'
    logs=[json.loads(line) for line in (inputs/'signals.jsonl').read_text(encoding='utf-8').splitlines()]
    logindex={(r['csymbol'],r['bar_eob']):r for r in logs}
    detailed_parity=[]
    for row in datasets['state_trace_control']:
        live=logindex[(row['csymbol'],row['eob'])];stored=live.get('entry') or {};entry=json.loads(row['entry']) if row.get('entry') else {}
        permission=json.loads(row['permission']) if row.get('permission') else None
        live_permission=live['facts']['trade_permission']
        checks={k:entry.get(k)==stored.get(k) for k in ['accepted','direction','reason','path','trigger_bar_id','evidence_id']}
        # Native log serializes candidates as repr; do not claim unlogged anchor identity parity.
        checks['permission_all_fields']=permission==live_permission
        detailed_parity.append(dict(csymbol=row['csymbol'],eob=row['eob'],**checks,anchor='NOT_LOGGED_NOT_CLAIMED',all_checked_fields_match=all(checks.values())))
    csvwrite(out,'baseline_detailed_parity.csv',detailed_parity)
    quotes=[json.loads(line) for line in (inputs/'quotes.jsonl').read_text(encoding='utf-8').splitlines()]
    quote_audit=[];rules=read(inputs/'strategy.json')['rules']
    for mode in ['control','ordered']:
        for row in datasets['state_trace_'+mode]:
            e=json.loads(row['entry']) if row.get('entry') else None
            if not e or not e.get('candidates'):continue
            log=logindex[(row['csymbol'],row['eob'])];f=log['facts'];anchor=json.loads(row['anchor']) if row.get('anchor') else None
            evidence_clock=json.loads(row['clock']) if row.get('clock') else None
            observed=datetime.fromisoformat(log['order_evaluated_at'])
            expires=datetime.fromisoformat(evidence_clock['expires_at']) if evidence_clock else datetime.fromisoformat(row['eob'])
            proxy=None
            for qr in quotes:
                at=datetime.fromisoformat(qr['local_received_at']);q=qr['quote']
                if qr['symbol']!=f['symbol'] or not observed<=at<=expires:continue
                age=(at-datetime.fromisoformat(q['created_at'])).total_seconds()
                if not (-1<=age<=3 and q.get('bid',0)>0 and q.get('ask',0)>=q['bid'] and q.get('tick',0)>0 and q['ask']-q['bid']<=4*q['tick']+1e-8):continue
                if q.get('ask_size' if e['direction']=='bull' else 'bid_size',0)<1:continue
                proxy=q['ask' if e['direction']=='bull' else 'bid'];break
            audit=dict(mode=mode,csymbol=row['csymbol'],eob=row['eob'],path=e.get('path'),strategy_accepted=e['accepted'],quote_reference=proxy,original_evidence_expiry=expires.isoformat(),reference_evaluation_after_expiry=observed>expires,reference_clock='ORIGINAL_OBSERVED_COORDINATOR_NOT_NEW_CANDIDATE_LATENCY',candidate_latency='UNKNOWN',calendar='BLOCK',execution_supported=False)
            if proxy is None or anchor is None:audit['stop_math']='UNKNOWN_MISSING_REFERENCE_OR_ANCHOR'
            else:
                spec=read(inputs/row['csymbol'].replace('.','_')/'metadata.json')['spec']
                try:
                    plan=build_stop_plan(direction=e['direction'],entry=float(proxy),anchor=float(anchor['anchor_price']),atr=f['atr14'],tick=spec['price_tick'],buffer_atr=rules['stop_buffer_atr'],min_atr=rules['stop_min_atr'],max_atr=rules['stop_max_atr'])
                    audit.update(stop_math='REFERENCE_MATH_ONLY_NOT_EXECUTION_PROOF',stop=plan.stop,target=plan.target)
                except ValueError as error:audit['stop_math']='REJECT:'+str(error)
            quote_audit.append(audit)
    csvwrite(out,'quote_stop_reference_audit.csv',quote_audit)
    technical=[]
    for mode in ['control','ordered']:
        traces=datasets['state_trace_'+mode];events={};selected=0;accepted=0
        for r in traces:
            e=json.loads(r['entry']) if r.get('entry') else None
            if not e:continue
            selected+=e.get('path')=='B';accepted+=bool(e.get('accepted'))
            for candidate in e.get('candidates',[]):
                if candidate['path']=='B':events[(r['csymbol'],candidate['trigger_bar_id'])]=1
        technical.append(dict(mode=mode,unique_B_technical_events=len(events),selected_B_frames=selected,all_paths_strategy_accepted_frames=accepted,calendar_eligible_orders=0,model_fills=0))
    write(out,'mechanism_comparison.json',dict(baseline_frames=54,baseline_logged_parity=matches,
          state_replay_status='PASS' if matches==54 and all(r['all_checked_fields_match'] for r in detailed_parity) else 'BASELINE_MISMATCH_STOP_INTERPRETATION',
          baseline_detailed_match_frames=sum(r['all_checked_fields_match'] for r in detailed_parity),
          modes=technical,changed_frames=len(datasets['candidate_deltas']),
          original_cases_by_new_contract=dict(Counter(r['classification'] for r in cases)),
          event_deltas=dict(Counter(r['classification'] for r in event_deltas)),
          merged_repeated_trigger=sum(r['merged_repeated_trigger'] for r in event_deltas),
          hist_advisory_enabled=False,account_feedback='NONE_CALENDAR_FORBIDDEN_NOT_FABRICATED',
          stop_quote=dict(reference_rows=len(quote_audit),reference_math_pass=sum(r['stop_math']=='REFERENCE_MATH_ONLY_NOT_EXECUTION_PROOF' for r in quote_audit),execution_proof='NOT_EVALUATED_CALENDAR_BLOCK',candidate_processing_latency='UNKNOWN'),profitability='NOT_EVALUATED',
          scope='Shared causal warmup, then independently advanced core books; ordered M5 lifecycle starts 45m before scope, lifespan6 M5; no future final checkpoint used'))
    print(json.dumps(read(out/'mechanism_comparison.json'),ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path.cwd());p.add_argument('--output',type=Path,required=True);a=p.parse_args();run(a.root,a.output)
