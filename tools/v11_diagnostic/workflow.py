"""Gated manual arithmetic and one original-tool invocation. No strategy accounts."""
from pathlib import Path
import argparse,importlib.util,pickle,statistics,subprocess,sys
from datetime import datetime,timezone,timedelta
import numpy as np
import pandas as pd
from .contracts import ROOT,EXPECTED,read,write,sha,validate_freeze,queue_identity,fresh_output
from .summarize import run as summarize

CN=timezone(timedelta(hours=8));NS=60_000_000_000


def load_tool(tool_dir):
    p=Path(tool_dir)/'diagnose.py'
    if sha(p)!=EXPECTED['diagnose']:raise ValueError('original diagnose.py bytes mismatch')
    spec=importlib.util.spec_from_file_location('v11_original_diagnose',p)
    tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
    for name,h in tool.SOURCE_HASHES.items():
        if sha(ROOT/name,True)!=h:raise ValueError('original V10 source changed: '+name)
    return tool


def original_data(prepared):
    prepared=Path(prepared)
    if sha(prepared)!=EXPECTED['prepared'] or sha(prepared.parent/'input_lock.json')!=EXPECTED['input_lock']:
        raise ValueError('original prepared/input_lock bytes not found')
    # Only the already authorized, exact-hash local pickle is deserialized.
    with prepared.open('rb') as f:return pickle.load(f)


def own_signature(starts):
    lengths=[]
    for i,t in enumerate(starts):
        if i==0 or t-starts[i-1]!=NS:lengths.append(1)
        else:lengths[-1]+=1
    return tuple(lengths)


def arithmetic(tool,leg,clock,row,record_prices=True):
    signal=pd.Timestamp(row.signal_eob).value;entry=pd.Timestamp(row.entry_bob).value
    idx=int(clock.ends.searchsorted(signal,side='right'))
    if idx>=len(clock.ends) or int(clock.starts[idx])!=entry or clock.dates[idx]!=row.entry_day:raise ValueError('legal entry mismatch in hand case')
    end=clock.expiry(idx)
    if end is None or end<entry:return dict(manual_ratio=None,program_ratio=None,reason='DEADLINE_UNKNOWN',reference_windows=[])
    starts=[int(t) for t in clock.starts if entry<=t<end];signature=own_signature(starts)
    local=pd.Timestamp(entry,tz='UTC').tz_convert('Asia/Shanghai')
    chosen=[]
    # Independent implementation: explicit starts and block counts, no tool.signature/span.
    for j in range(idx-1,-1,-1):
        t=clock.local[j]
        if (t.hour,t.minute)!=(local.hour,local.minute):continue
        finish=clock.expiry(j)
        if finish is None or finish>signal or finish<=clock.starts[j]:continue
        subset=clock.starts[j:clock.starts.searchsorted(finish)]
        if own_signature(subset)==signature:
            chosen.append((j,int(clock.starts.searchsorted(finish))))
            if len(chosen)==20:break
    fact=leg['facts'].get(signal)
    risk=None if fact is None else (fact['core']['close']-row.stop)*(1 if row.direction=='bull' else -1)
    reason='OLD_WINDOW_EVENT_TIME_PROXY';spans=[];refs=[]
    if fact is None or not fact.get('eligible'):reason='NO_SIGNAL_FACT'
    elif risk is None or not np.isfinite(risk) or risk<=0:reason='INVALID_SIGNAL_R0'
    elif not signature:reason='EMPTY_WINDOW'
    elif len(chosen)<20:reason='FEWER_THAN_20_WINDOWS'
    if reason=='OLD_WINDOW_EVENT_TIME_PROXY':
        for a,b in chosen:
            indices=[leg['index'].get(int(t)) for t in clock.ends[a:b]]
            info=dict(start_ns=int(clock.starts[a]),end_ns=int(clock.starts[b-1]+NS),expected_minutes=b-a,
                received_minutes=sum(i is not None for i in indices),ended_no_later_than_signal=bool(clock.starts[b-1]+NS<=signal))
            if None in indices:reason='MISSING_HISTORICAL_MINUTE'
            else:
                # Prices are read only for these already closed reference windows.
                high=[float(leg['values'][i,1]) for i in indices];low=[float(leg['values'][i,2]) for i in indices]
                if not all(np.isfinite(high)) or not all(np.isfinite(low)) or any(h<l for h,l in zip(high,low)):reason='INVALID_HISTORICAL_PRICE'
                else:
                    span=max(high)-min(low);spans.append(span)
                    if record_prices:info.update(high=max(high),low=min(low),span=span)
            refs.append(info)
    manual=statistics.median(spans)/risk if reason=='OLD_WINDOW_EVENT_TIME_PROXY' and len(spans)==20 else None
    ratio,status=tool.measure_space(leg,clock,idx,signal,end,row.stop,row.direction)
    program=None if not np.isfinite(ratio) else float(ratio)
    if status!=reason or (manual is None)!=(program is None) or (manual is not None and not np.isclose(manual,program,rtol=1e-12,atol=1e-12)):
        raise ValueError('manual/program space mismatch; STOP BEFORE FULL RUN')
    if len(starts)!=clock.span(entry,end):raise ValueError('manual/program minutes mismatch')
    return dict(manual_ratio=manual,program_ratio=program,reason=reason,remaining_minutes=len(starts),
        signature=list(signature),signal_close=None if fact is None else fact['core']['close'],stop=row.stop,R0=risk,
        signal_ns=int(signal),entry_ns=int(entry),deadline_ns=int(end),reference_windows=refs,
        known_history_only=True,any_future_path_read=False)


def manual(prereg,receipts,tool_dir,prepared,out):
    reg,binding=validate_freeze(prereg,receipts);output=fresh_output(out,True)
    tool=load_tool(tool_dir);data=original_data(prepared)
    from tools.short_swing_v9.policy import HoldingCalendar
    clocks=tool.public_clocks(ROOT);policies=HoldingCalendar(clocks).products
    clocks={cs:tool.Clock(clock,policies[cs]) for cs,clock in clocks.items()}
    # Select IDs from stable metadata only; no gross R, exit outcome or price path is loaded.
    columns=['trade_id','csymbol','symbol','entry_day','signal_eob','entry_bob','direction','stop']
    ledger=pd.read_csv(ROOT/'reports/v10_same_discipline/GROSS_ORDINARY_SHORT_trades.csv',usecols=columns).sort_values('trade_id')
    selections={};rows=list(ledger.itertuples(index=False))
    for row in rows:
        clock=clocks[row.csymbol];idx=int(clock.ends.searchsorted(pd.Timestamp(row.signal_eob).value,side='right'));end=clock.expiry(idx)
        if end is None:continue
        signature=own_signature(clock.starts[idx:clock.starts.searchsorted(end)])
        if len(signature)>1 and 'CROSS_SESSION' not in selections:selections['CROSS_SESSION']=row
        leg=data['legs'][row.symbol];days={d for t,d in zip(clock.starts,clock.dates) if clock.starts[idx]<=t<=end}
        if any(leg['selection'].get(d)!=row.symbol for d in days) and 'ROLL_BOUNDARY' not in selections:selections['ROLL_BOUNDARY']=row
        if len(selections)==2:break
    cases=[]
    for kind in ('CROSS_SESSION','ROLL_BOUNDARY'):
        row=selections.get(kind)
        if row is None:cases.append(dict(kind=kind,status='CASE_TYPE_ABSENT'));continue
        calc=arithmetic(tool,data['legs'][row.symbol],clocks[row.csymbol],row)
        cases.append(dict(kind=kind,status='ARITHMETIC_MATCH',trade_id=int(row.trade_id),symbol=row.symbol,**calc))
    if not any(c.get('manual_ratio') is not None for c in cases):
        # Stable ID scan uses historical completeness, never profitability.
        for row in rows:
            calc=arithmetic(tool,data['legs'][row.symbol],clocks[row.csymbol],row,False)
            if calc['manual_ratio'] is not None:
                calc=arithmetic(tool,data['legs'][row.symbol],clocks[row.csymbol],row)
                cases.append(dict(kind='FIRST_NUMERIC_BY_INPUT_COMPLETENESS',status='ARITHMETIC_MATCH',trade_id=int(row.trade_id),symbol=row.symbol,**calc));break
        else:cases.append(dict(kind='FIRST_NUMERIC_BY_INPUT_COMPLETENESS',status='NO_NUMERIC_BRANCH_IN_INPUTS'))
    output.mkdir(parents=True)
    write(output/'manual_checks.json',dict(state='PASSED_SELF_ARITHMETIC',**binding,originals=EXPECTED,cases=cases,
        identities=['Codex','Codex'],independent_arithmetic_algorithm=True,independent_person=False,
        checked_at_beijing=datetime.now(CN).isoformat(),gross_r_or_future_path_read=False))
    return dict(status='PASSED_SELF_ARITHMETIC',cases=len(cases),numeric_cases=sum(c.get('manual_ratio') is not None for c in cases))


def full(prereg,receipts,tool_dir,prepared,manual_dir,out):
    reg,binding=validate_freeze(prereg,receipts);output=fresh_output(out,True)
    check=read(Path(manual_dir)/'manual_checks.json')
    if check.get('state')!='PASSED_SELF_ARITHMETIC' or check.get('manifest_sha256')!=binding['manifest_sha256'] or check.get('originals')!=EXPECTED:
        raise ValueError('manual arithmetic receipt missing or stale')
    review=read(Path(receipts)/'hand_review.json')
    if review.get('state')!='SELF_REVIEWED_NO_INDEPENDENT_PERSON' or review.get('manual_checks_sha256')!=sha(Path(manual_dir)/'manual_checks.json') or review.get('manifest_sha256')!=binding['manifest_sha256']:
        raise ValueError('manual case review hash missing or stale')
    tool=load_tool(tool_dir)
    if sha(prepared)!=EXPECTED['prepared'] or sha(Path(prepared).parent/'input_lock.json')!=EXPECTED['input_lock']:raise ValueError('original hash mismatch')
    registry=ROOT/'data/cache/v11_restricted';registry.mkdir(parents=True,exist_ok=True)
    token=registry/'one_full_diagnostic.json'
    # No attempt is recorded until source/signoff/hand-case gates have passed.
    with token.open('x',encoding='utf8') as stream:
        import json
        json.dump(dict(state='STARTED',manifest_sha256=binding['manifest_sha256'],originals=EXPECTED,
            started_at_beijing=datetime.now(CN).isoformat(),same_bytes_repair_attempts=0),stream)
    output.mkdir(parents=True);diagnosis=output/'diagnosis'
    command=[sys.executable,str(Path(tool_dir)/'diagnose.py'),'--repo',str(ROOT),'--prepared',str(prepared),'--out',str(diagnosis)]
    result=subprocess.run(command,capture_output=True,text=True,encoding='utf8',errors='replace')
    (output/'original_tool_stdout.txt').write_text(result.stdout,encoding='utf8');(output/'original_tool_stderr.txt').write_text(result.stderr,encoding='utf8')
    if result.returncode:
        v=read(token);v.update(state='FAILED_REQUIRES_DOCUMENTED_SAME_BYTES_REPAIR',returncode=result.returncode);write(token,v)
        raise ValueError('original diagnostic failed; private stderr preserved; no automatic rerun')
    f=pd.read_csv(diagnosis/'audit.csv')
    write(diagnosis/'provenance.json',dict(data_kind='REAL_OLD_WINDOW',originals=EXPECTED,**binding,
        original_paths=dict(prepared=str(Path(prepared).resolve()),input_lock=str(Path(prepared).resolve().parent/'input_lock.json')),
        audit_sha256=sha(diagnosis/'audit.csv'),queue_identity_sha256=queue_identity(f),
        manual_checks_sha256=sha(Path(manual_dir)/'manual_checks.json'),run_token_sha256=sha(token)))
    decision=summarize(diagnosis,prereg,receipts,output/'summary')
    v=read(token);v.update(state='COMPLETE',finished_at_beijing=datetime.now(CN).isoformat(),status=decision['status'],
        summary_manifest_sha256=sha(output/'summary/manifest.json'));write(token,v)
    return dict(status=decision['status'],processed=decision['mother_cohort_processed'],independent_test_consumed=False)


def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['manual','full']);p.add_argument('--prereg',type=Path,required=True)
    p.add_argument('--receipts',type=Path,required=True);p.add_argument('--tool-dir',type=Path,required=True);p.add_argument('--prepared',type=Path,required=True)
    p.add_argument('--manual-dir',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    if a.phase=='manual':r=manual(a.prereg,a.receipts,a.tool_dir,a.prepared,a.out)
    else:
        if a.manual_dir is None:p.error('--manual-dir required')
        r=full(a.prereg,a.receipts,a.tool_dir,a.prepared,a.manual_dir,a.out)
    import json
    print(json.dumps(r,ensure_ascii=False))


if __name__=='__main__':main()
