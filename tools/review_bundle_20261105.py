"""Generate whitelisted derived review evidence; private logs and accounts stay local."""
from pathlib import Path
from datetime import datetime,timedelta,timezone
from collections import Counter
import argparse,csv,hashlib,json
TZ=timezone(timedelta(hours=8));START=datetime(2026,11,5,21,tzinfo=TZ);END=datetime(2026,11,6,2,30,tzinfo=TZ)
def clock(v):return datetime.fromisoformat(str(v).replace('Z','+00:00'))
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def logs(p):
    raw=p.read_bytes();out=[];parts=raw.decode('utf8').splitlines()
    for n,line in enumerate(parts):
        try:out.append(json.loads(line))
        except ValueError:
            if n!=len(parts)-1:raise
    return out,dict(source=p.name,bytes_read=len(raw),sha256=hashlib.sha256(raw).hexdigest(),scope='EXACT_READ_PREFIX',publication='PRIVATE_ORIGINAL_NOT_UPLOADED')
def write_csv(path,rows,columns):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(rows)
def main():
    p=argparse.ArgumentParser();p.add_argument('--client',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();o=a.output;o.mkdir(parents=True,exist_ok=True)
    root=a.client/'_strategy_runtime';ev=root/'evidence/paper_observation_five';qa=ev/'inspections';final=read(qa/'20261106_night_final_qa.local.json')
    signals,h1=logs(ev/'signals.jsonl');recons,h2=logs(ev/'position_recon.jsonl');events,h3=logs(ev/'runtime_events.jsonl');orders,h4=logs(ev/'order_events.jsonl')
    signals=[r for r in signals if START<clock(r['bar_eob'])<=END];facts=[];candidates=[]
    for r in signals:
        f=r['facts'];d=f['trend_decision'];e=r.get('entry') or {};i=f['trend_inputs'];pd=r.get('paper_decision');t=clock(r['order_evaluated_at'])
        facts.append(dict(csymbol=r['csymbol'],symbol=f['symbol'],m15_eob=r['bar_eob'],ema34=f['ema34'],atr14=f['atr14'],close_minus_ema34=f['close']-f['ema34'],close_side=i['close_side'],slope_state=f['slope_state'],trend_h1_absolute=f['h1_absolute'],trend_h1_relative=i['h1_relative'],trend_h1_divergence=i['h1_divergence'],entry_finalized_h1='NOT_PERSISTED_IN_SIGNAL_ROW',entry_finalized_h1_eob='NOT_PERSISTED_IN_SIGNAL_ROW',top_div=f['top_div'],bottom_div=f['bottom_div'],bearish_pattern=i['s2_bearish_pattern'],p_condition=i['p_close_cross'],prior=d['prior_state'],proposed_action=d['proposed_action'],confirmed=d['confirmed'],direction=d['confirmed_state'],leave_votes=d['state']['leave_votes'],reason=d['reason_code'],entry_accepted=e.get('accepted'),entry_path=e.get('path'),entry_reason=e.get('reason'),execution_allowed=bool(pd and pd[0]),execution_reason=pd[1] if pd and not pd[0] else 'NOT_ATTEMPTED' if not pd else 'INTENT_CREATED'))
        if e.get('accepted'):
            before=[x for x in recons if clock(x['at'])<=t];after=[x for x in recons if clock(x['at'])>t]
            candidates.append(dict(csymbol=r['csymbol'],symbol=f['symbol'],signal_at=r['bar_eob'],ready_at=r['signal_ready_at'],evaluated_at=r['order_evaluated_at'],expires_at=(clock(r['bar_eob'])+timedelta(seconds=30)).isoformat(),path=e['path'],trigger_bar_id=e['trigger_bar_id'],allowed=bool(pd and pd[0]),first_block=pd[1] if pd and not pd[0] else '',prior_recon_at=before[-1]['at'] if before else 'UNKNOWN',next_recon_at=after[0]['at'] if after else 'UNKNOWN',seconds_to_next_recon=(clock(after[0]['at'])-t).total_seconds() if after else '',prior_positions=sum(int(p['volume'])>0 for p in before[-1]['positions']) if before else 'UNKNOWN',prior_active_orders=len(before[-1]['active']) if before else 'UNKNOWN',busy_rpc_op='UNKNOWN_NOT_RECORDED',m15_top_div=f['top_div'],m15_bottom_div=f['bottom_div']))
    coverage=[dict(csymbol=r['csymbol'],expected_m1=r['expected_m1'],received_unique_m1=r['unique_received_m1'],missing_m1=r['missing_m1'],duplicate_m1=r['duplicate_m1'],extra_m1=r['extra_m1'],expected_closed_m15=r['closed_m15_expected'],last_m15=r['last_closed_m15'],warmup_m15=r['warmup'].get('expected_m15'),warmup_missing_m1=r['warmup'].get('missing_m1'),warmup_engineering_qualified=r['warmup'].get('qualified'),strict_blind_sample_qualification='NOT_ASSESSED') for r in final['coverage']]
    receipts=[dict(actual_at=r['at'],receipt_file=r['file'],sha256=r['sha256'],issues='|'.join(r['issues'])) for r in final['inspection_receipts']]
    day=[r for r in events if r.get('at') and datetime(2026,11,5,tzinfo=TZ)<=clock(r['at'])<START]
    starts=[dict(at=r['at'],kind=r['kind'],code=r.get('code',''),scope='PRE_NIGHT_ENGINEERING_NOT_FULL_DAY_STRATEGY_RETURN') for r in day if r['kind'] in ('GM_PLATFORM_ERROR','GM_PLATFORM_HOST_STARTED','GM_PLATFORM_HOST_STOPPED')]
    tagged=Counter(r.get('intent',{}).get('tag','UNKNOWN') for r in orders if r.get('recorded_at') and datetime(2026,11,5,tzinfo=TZ)<=clock(r['recorded_at'])<START and r.get('kind')=='ORDER_INTENT')
    for name,rows in [('m15_decisions.csv',facts),('entry_candidates.csv',candidates),('coverage.csv',coverage),('actual_inspections.csv',receipts),('engineering_timeline.csv',starts)]:
        if rows:write_csv(o/name,rows,list(rows[0]))
    blocks=Counter(c['first_block'] for c in candidates)
    summary=dict(generated_at=datetime.now(TZ).isoformat(),basis='LOCAL_AUTHORIZED_LOGS_SINGLE_AGENT_SELF_CHECK',night_window=dict(start=START.isoformat(),end=END.isoformat()),night_totals=final['totals'],blocks_by_csymbol={cs:dict(Counter(c['first_block'] for c in candidates if c['csymbol']==cs)) for cs in sorted(set(c['csymbol'] for c in candidates))},pre_night_order_intents_by_tag=dict(tagged),pre_night_timeline_is_incomplete=True,positions_at_night_handoff=0,active_intents_at_night_handoff=final['status']['active_intents'],notifications_at_handoff=final['notifications'],recorded_night_platform_errors=len(final['recorded_night_platform_or_worker_errors']),recorded_night_rpc_failures=len(final['night_rpc_or_recon_failures']),m5_new_warning_count=final['m5_alert_count'],explicit_unexecuted_inspection_nodes=final['explicit_unexecuted_inspection_nodes'],inspection_timing='ACTUAL_TIMES_ONLY;DO_NOT_INFER_CONTINUOUS_MONITORING',core_fixes_deployed=False,notification_corrections_deployed=True,manual_other_sim_account_excluded=True,profitability_conclusion='NOT_EVALUABLE_ZERO_NATURAL_FILLS',independent_research_sample_consumed=False,privacy='NO_RAW_M1_TICKS_SCREENSHOTS_ACCOUNTS_OR_CREDENTIALS')
    assert len(facts)==38 and len(candidates)==5 and blocks==Counter(BROKER_BUSY=4,ONE_LOT_RISK_LIMIT=1)
    assert sum(c['received_unique_m1'] for c in coverage)==570 and sum(c['missing_m1'] for c in coverage)==0
    (o/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    manifest=dict(sources=[h1,h2,h3,h4,dict(source='night_final_qa.local.json',sha256=hashlib.sha256((qa/'20261106_night_final_qa.local.json').read_bytes()).hexdigest(),publication='PRIVATE_ORIGINAL_NOT_UPLOADED')],method='Only explicit whitelisted derived fields; hard cutoff excludes Nov6 morning events.',review_basis_commit='70310c3d',raw_data_redistributed=False)
    (o/'evidence_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(output=str(o),m15_rows=len(facts),candidate_rows=len(candidates),coverage_rows=len(coverage),inspection_rows=len(receipts),pre_night_timeline_rows=len(starts)),ensure_ascii=False))
if __name__=='__main__':main()
