"""Recompute original path facts from a frozen, previously observed batch. Offline only.

python -m tools.entry_compatibility.diagnose --batch <private frozen batch> --output <new dir>
No SDK import, network, order account, future price response or production state writes.
"""
import argparse
import csv
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import subprocess
import pandas as pd
from futures_trend_quant.core.models import Bar
from futures_trend_quant.core.entry.evidence import build_entry_evidence
from futures_trend_quant.core.entry.gate import evaluate_momentum_gate
from futures_trend_quant.core.entry.models import EntryContext
from futures_trend_quant.core.entry.paths import evaluate_entries
from futures_trend_quant.core.pipeline.step import _entry_settings
from futures_trend_quant.live.paper_data import complete_bars
from .conditions import path_conditions, relation
from .policy import evaluate_selected, POLICY

START=pd.Timestamp('2026-11-06T21:00:00+08:00')
END=pd.Timestamp('2026-11-07T02:30:00+08:00')


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(out,name,value):
    (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str,allow_nan=False)+'\n',encoding='utf-8')


def csvwrite(out,name,rows):
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with (out/name).open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader()
        for row in rows:w.writerow({k:json.dumps(v,ensure_ascii=False,default=str) if isinstance(v,(dict,list,tuple)) else v for k,v in row.items()})


def run(batch,out):
    if out.exists():raise ValueError('new output directory required; no overwrite')
    manifest=read(batch/'input_manifest.local.json');inputs=batch/'inputs'
    for item in manifest['files']:
        assert hashlib.sha256((inputs/item['path']).read_bytes()).hexdigest()==item['sha256']
    out.mkdir(parents=True)
    rules=read(inputs/'strategy.json')['rules'];settings=_entry_settings(rules)
    records=[json.loads(line) for line in (inputs/'signals.jsonl').read_text(encoding='utf-8').splitlines()]
    records=[r for r in records if START<pd.Timestamp(r['bar_eob'])<=END]
    records.sort(key=lambda r:(r['bar_eob'],r['csymbol']))
    assert len(records)==54
    matrix=[];relations=[];diffs=[];bars_by_cs={};previous_ids=set();first_seen={}
    for cs in sorted({r['csymbol'] for r in records}):
        key=cs.replace('.','_');raw=pd.read_parquet(inputs/key/'startup_cache.parquet').to_dict('records')
        raw += [item for line in (inputs/key/'observed.jsonl').read_text(encoding='utf-8').splitlines()
                if START<pd.Timestamp((item:=json.loads(line))['eob'])<=END]
        bars={}
        for r in raw:
            end=pd.Timestamp(r['eob']).to_pydatetime()
            if end>END:continue
            b=Bar(r['symbol'],'60s',pd.Timestamp(r['bob']).to_pydatetime(),end,
                  *[float(r[k]) for k in ('open','high','low','close')],float(r.get('volume',0)))
            if end in bars:assert bars[end]==b
            bars[end]=b
        allbars=sorted(bars.values(),key=lambda b:b.eob)
        bars_by_cs[cs]=(allbars,complete_bars(allbars,5),complete_bars(allbars,15))
    for r in records:
        cs=r['csymbol'];f=r['facts'];p=f['trade_permission'];direction=p['background'];selected=r.get('entry') or {}
        header=dict(csymbol=cs,eob=r['bar_eob'],direction=direction,selected=selected.get('path'),original_reason=selected.get('reason'))
        if direction not in ('bull','bear'):
            for path in 'ABCD':matrix.append(dict(**header,path=path,status='NOT_EVALUATED_NO_DIRECTION',missing=['NO_DIRECTION_AUTHORIZATION'],directional_permission='NOT_APPLICABLE'))
            continue
        cutoff=pd.Timestamp(r['bar_eob']);m1,m5,m15=bars_by_cs[cs]
        a=[b for b in m1 if b.eob<=cutoff][-1200:];b=[b for b in m5 if b.eob<=cutoff][-1200:];c=[b for b in m15 if b.eob<=cutoff][-1200:]
        gate=evaluate_momentum_gate(c,direction,enabled=rules['gate_enabled'],distance_atr=rules['gate_distance_atr'],shrink_bars=rules['gate_hist_shrink_bars'])
        evidence=build_entry_evidence(b,a,direction,div_mode=rules['div_mode'],m5_lookback=rules['m5_flat_lookback'],flat_ratio=rules['m5_flat_slope_ratio'],position_atr=rules['evidence_position_atr'],pullback_atr=rules['pullback_min_atr'],consolidation_bars=rules['consolidation_bars'],consolidation_range_atr=rules['consolidation_range_atr'],m1_lookback=rules['m1_evidence_lookback'])
        ctx=EntryContext(direction,True,f['h1_absolute'],f['slope_state'],f['top_div'] if direction=='bull' else f['bottom_div'],gate.is_open)
        original=evaluate_entries(ctx,evidence,settings)
        assert original.path==selected.get('path'),(cs,cutoff,'path mismatch',original,selected)
        checks=path_conditions(ctx,evidence,settings)
        for path,conditions in checks.items():
            raw=next((x for x in original.candidates if x.path==path),None)
            seen=first_seen.setdefault((cs,raw.evidence_id),r['bar_eob']) if raw else None
            matrix.append(dict(**header,path=path,status='RAW_PATH' if all(conditions.values()) else 'MISSING_CONDITIONS',conditions=conditions,missing=[k for k,v in conditions.items() if not v],h1=f['h1_absolute'],gate=asdict(gate),m5_evidence=asdict(evidence),path_trigger_eob=None if raw is None else raw.trigger_eob,first_observed_in_scope=seen,first_ever_path_eligible='UNKNOWN_NOT_FULL_PREFIX_REPLAY',original_selected_clock=f.get('evidence_clock'),permission_allowed=p['new_entry_allowed'],permission_reasons=p['reasons'],downstream='NOT_EVALUATED'))
        for path,first,second in [('B',evidence.m5_pullback,evidence.m5_pattern),('C',evidence.m5_consolidation,evidence.m5_breakout or evidence.m5_pattern)]:
            if not (first and second):continue
            rel=relation(first,second)
            pair=cs+'|'+path+'|'+first.bar_id+'|'+second.bar_id
            later=[x for x in b if x.eob>second.eob]
            level=next(x.low if direction=='bull' else x.high for x in b if x.identity==second.bar_id)
            broken=any(x.low<level if direction=='bull' else x.high>level for x in later)
            relations.append(dict(**header,path=path,**rel,selected_this_path=original.path==path,pair_reused_from_previous_frame=pair in previous_ids,confirmation_bar_extreme_broken_before_evaluation=broken,break_test='DESCRIPTIVE_ONLY_NOT_ORIGINAL_INVALIDATION_RULE',trigger_bar=original.trigger_bar_id if original.path==path else None))
            previous_ids.add(pair)
        if original.path:
            hist=(r.get('trend_observation') or {})['momentum_hist']
            kw=dict(close_side=f['trend_inputs']['close_side'],slope=f['slope_state'],hist=hist,opposite_divergence=ctx.m15_reverse_divergence,closed_h1_known=f['h1_absolute'] is not None,calendar_qualified=bool(f['h1_source']['calendar_qualified']),previous_weak=0,changed=False,eob=r['bar_eob'])
            control=evaluate_selected(original.path,direction,**kw)
            candidate=evaluate_selected(original.path,direction,enabled=True,**kw)
            assert control.reasons==tuple(p['reasons'])
            diffs.append(dict(**header,evidence_id=original.evidence_id,hist=hist,control=asdict(control),candidate=asdict(candidate),control_allowed=control.new_entry_allowed,candidate_allowed=candidate.new_entry_allowed,executable=False,friday_block='PRE_LONG_CLOSURE_NO_REOPEN',event_mechanism_qualified=False))
    csvwrite(out,'path_condition_matrix.csv',matrix);csvwrite(out,'event_relation_audit.csv',relations);csvwrite(out,'static_permission_diff.csv',diffs)
    passed=[dict(csymbol=r['csymbol'],eob=r['bar_eob']) for r in records if r['facts']['trade_permission']['new_entry_allowed']]
    assert len(passed)==1 and passed[0]['csymbol']=='DCE.M' and '21:15:' in passed[0]['eob']
    assert len(diffs)==7 and sum(x['candidate_allowed'] for x in diffs)==4
    selected_b=[x for x in relations if x['path']=='B' and x['selected_this_path']]
    summary=dict(frames=54,no_direction=22,no_path=25,shared_block=7,permission_passed=passed,path_and_permission_intersection=0,selected_b_relation_order=dict(Counter(x['ordering'] for x in selected_b)),b_selected=len(selected_b),mechanism_qualified_b=0,static_candidate_permission_passed=4,account_phase='BLOCKED_EVENT_CONTRACT_UNQUALIFIED',state_replay='NOT_RUN_NOT_SUBSTITUTED_BY_LOG_MATRIX',production_changed=False)
    write(out,'permission_overlap.json',summary)
    write(out,'candidate_contract.json',dict(policy=POLICY,enabled=False,production_call_sites=0,only_change='Selected B hist sign advisory; original numeric hist retained',unchanged=['A/C/D','selection priority','technical exit','risk','30s deadline','calendar','quote','anchor'],unknown_hist='reject',release='BLOCKED_EVENT_CONTRACT_UNQUALIFIED'))
    write(out,'baseline_manifest.json',dict(evidence_commit='45b11bad4b4a03574906eaa133174187c7572f4f',code_parent=subprocess.check_output(['git','rev-parse','HEAD']).decode().strip(),input_manifest_sha256=hashlib.sha256((batch/'input_manifest.local.json').read_bytes()).hexdigest(),input_files_verified=len(manifest['files']),scope='SEEN_20261106_NIGHT_ONLY',new_data_requests=False,review='SELF_CHECK',full_state_replay=False))
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--batch',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();run(args.batch,args.output)
