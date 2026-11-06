"""Export sanitized derived SEEN-case explanations; never raw M1 or credentials."""
from pathlib import Path
from collections import Counter
from datetime import datetime,timedelta
import argparse,html,json,shutil
from tools.futures_trend_review import read,write,write_csv,sha,load_minutes,POOL


def export(source,target):
    summary=read(source/'review_summary.local.json');rows=read(source/'all_states.local.json')
    # Fix metadata conservatively. A closing M1 receipt is not automatically the
    # first availability of all fifteen source minutes. Preserve original rows.
    input_receipts={}
    for cs in POOL:
        minutes,receipts,_=load_minutes(source,cs);input_receipts[cs]=receipts
    for r in rows:
        end=datetime.fromisoformat(r['bar_eob']);receipts=input_receipts[r['csymbol']]
        times=[end-timedelta(minutes=i) for i in range(15)]
        r['first_received_at']=max((receipts[t] for t in times),key=lambda t:datetime.fromisoformat(t)) if all(t in receipts for t in times) else None
        r['known_at_basis']='MAX_DIRECT_FIRST_M1_RECEIPTS_WHEN_ALL_15_KNOWN;OTHERWISE_UNKNOWN;NOT_DOWNLOAD_CLOCK'
        if r['computed_at']=='REPLAY_COMPUTED_SEPARATELY':
            r['computed_at']=None;r['computed_at_basis']='PER_ROW_COMPUTATION_CLOCK_NOT_RETAINED_BY_FIRST_EXPORTER'
    write(source/'all_states.normalized.local.json',rows)
    target.mkdir(parents=True,exist_ok=True)
    fields=['csymbol','actual_symbol','bar_eob','close','ema34','atr14','production_m15_background','h1_structure_class',
       'local_direction','local_direction_basis','momentum_direction','momentum_state','momentum_hist','phase','phase_definition_version',
       'episode_id','episode_known_at','range_id','warning_active','entry_veto_active','observer_eligible','production_permission',
       'shadow_permission','shadow_reason_codes','divergence_events','h1_source','is_closed','is_complete','data_quality',
       'first_received_at','known_at_basis','computed_at','computed_at_basis','recorded_permission_equal','entry','recorded_entry']
    write_csv(target/'all_states.csv',[{k:r.get(k) for k in fields} for r in rows])
    write_csv(target/'state_trace.csv',[dict(csymbol=r['csymbol'],eob=r['bar_eob'],phase=r['phase'],
        background=r['production_m15_background'],h1=r['h1_structure_class'],momentum=r['momentum_state'],local_direction=None,
        permission=r['production_permission'],source_bar_ids=r['source_bar_ids'],episode=r['episode_id']) for r in rows])
    for name in ('divergence_veto_audit.csv','candidate_coverage.csv'):
        shutil.copyfile(source/name,target/name)
    baseline=read(source/'baseline_lock.json');baseline['actual_host']={k:v for k,v in baseline['actual_host'].items() if k in ('entry_sha256_lf','host_sha256_lf','config_sha256','at','live_orders_enabled')}
    write(target/'baseline_lock.json',baseline)
    manifest=read(source/'input_manifest.json')
    manifest['allowed_inputs']=[{k:v for k,v in x.items() if k not in ('path','original_path')} for x in manifest['allowed_inputs']]
    annex=read(source/'input_annex.json');annex['files']=[{k:v for k,v in x.items() if k not in ('path','original_path')} for x in annex['files']]
    manifest['initial_cache_annex']=annex;write(target/'input_manifest.json',manifest)
    cases=read(source/'candidate_coverage.local.json');response_counts={}
    for n in ('15','30','60','120'):
        good=[c['responses'][n] for c in cases if c['responses'][n]['status']=='COMPLETE_DESCRIPTIVE_ONLY']
        response_counts[n]=dict(complete=len(good),unknown=len(cases)-len(good),
            mean_after_next_open_displacement=None if not good else sum(c['after_next_open_displacement'] for c in good)/len(good),
            unit='product price points pooled only for coverage; cross-product mean not economically comparable')
    # Price-point means are not pooled across contracts for inference.
    for n in response_counts:response_counts[n].pop('mean_after_next_open_displacement')
    summary['response_coverage']=response_counts
    summary['permission_comparisons']=sum(p['recorded_permission_comparisons'] for p in summary['products'])
    summary['permission_mismatches']=sum(p['recorded_permission_mismatches'] for p in summary['products'])
    summary['final_trend_matches']=sum(p['final_trend_equal'] for p in summary['products'])
    summary['phase_counts']=dict(Counter(r['phase'] for r in rows))
    technical={r['entry']['evidence_id']:r['entry'] for r in rows if r['entry'] and r['entry'].get('evidence_id')}
    summary['unique_flat_diagnostic_technical_evidence']=len(technical)
    summary['unique_flat_diagnostic_paths']=dict(Counter(e['path'] for e in technical.values()))
    summary['recorded_candidate_rows']=sum(bool(r['recorded_entry'] and r['recorded_entry']['accepted']) for r in rows)
    summary['permission_candidate']='NOT_REGISTERED;EXPLANATION_ONLY'
    for p in summary['products']:p.pop('missing_intervals',None)
    write(target/'summary.json',summary)
    checks=[]
    for name in ('final_targeted_tests.local.xml','final_contract_tests.local.xml'):
        q=source/name
        if q.exists():checks.append(dict(file=name,sha256=sha(q)))
    write(target/'causality_report.json',dict(explanation_only=True,core_sha256_lf=summary['core_sha256_lf'],
        permission_copied_unchanged=True,recorded_comparisons=summary['permission_comparisons'],mismatches=summary['permission_mismatches'],
        tests=checks,EA_dependency=False,new_parameters=False,review='SINGLE_AGENT_SELF_CHECK',
        source_gap_batch_preserved=True,original_clock_unknown_not_imputed=True,independent_sample_consumed=False))
    write_csv(target/'restart_parity.csv',[dict(csymbol=p['csymbol'],actual_last_eob_matches=p['same_last_eob'],
        final_confirmed_trend_matches=p['final_trend_equal'],recorded_permission_comparisons=p['recorded_permission_comparisons'],
        mismatches=p['recorded_permission_mismatches'],scope='FROZEN_SEED_PLUS_ALL_CASE_INPUT_CACHES') for p in summary['products']])
    write_csv(target/'warmup_overlap.csv',[dict(csymbol=p['csymbol'],required_complete_m15=1200,
        complete_m15_before_first_case=p.get('complete_m15_before_first_case'),available_prefix_m15=p.get('input_prefix_m15',p['m15']),
        independent_rolling_origin_test='NOT_RUN;PERSISTED_STATE_NOT_RESET',restart_check='FROZEN_ORIGIN_REBUILD_VS_ACTUAL_CHECKPOINT') for p in summary['products']])
    gold=[r for r in rows if r['csymbol']=='SHFE.AU'];special={'2026-11-05T22:45:00+08:00','2026-11-06T00:00:00+08:00','2026-11-06T00:15:00+08:00','2026-11-06T01:00:00+08:00'}
    body=[]
    for r in gold:
        p=r['production_permission'];cls='focus' if r['bar_eob'] in special else ''
        cells=[r['bar_eob'][5:16].replace('T',' '),f"{r['close']:.2f}",f"{r['ema34']:.2f}",r['h1_structure_class'],
            r['production_m15_background'],r['momentum_state'],r['phase'],'UNKNOWN',
            '允许继续执行检查' if p['new_entry_allowed'] else '关闭',','.join(p['reasons']),p['technical_exit_direction'] or '无']
        body.append('<tr class="'+cls+'">'+''.join('<td>'+html.escape(str(x))+'</td>' for x in cells)+'</tr>')
    page='''<!doctype html><meta charset="utf-8"><title>沪金闭合事实与权限解释</title><style>body{font:15px system-ui;margin:28px;color:#152238}table{border-collapse:collapse;font-size:13px}td,th{padding:9px;border:1px solid #ccd4de}.focus{background:#fff1c6}th{background:#e6edf6}</style><h1>au2612：国内期货闭合事实与权限解释</h1><p>固定SEEN工程案例；非EA对照、非收益验证。局部结构方向尚无独立依据，保持UNKNOWN；短动能不冒充新趋势。高亮为用户预先指定的三次旧VETO和01:00 A时点，表内为当前V2同源诊断。</p><table><thead><tr>'''+''.join('<th>'+x+'</th>' for x in ['确认柱','收盘','EMA34','H1结构','S0背景','短动能','阶段','局部方向','生产新开','原权限原因','技术退出方向'])+'</tr></thead><tbody>'+''.join(body)+'</tbody></table><p>原件、缓存和凭据留本机。H1为会话混合时长序列；解释改善不代表预测力改善。执行与复核均为同一AI自检。</p>'
    (target/'gold_case_comparison.html').write_text(page,encoding='utf8')
    print(json.dumps(dict(rows=len(rows),permission_comparisons=summary['permission_comparisons'],mismatches=summary['permission_mismatches'],final_trend_matches=summary['final_trend_matches'],gold_rows=len(gold))),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();export(a.source,a.output)
