"""Normal research CLI: admit -> freeze as-of keys -> optionally score once."""
from pathlib import Path
import argparse,json,sys
from bisect import bisect_left
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,digest
from tools.first_entry_v71.admission import admit,DEFAULT_LOCK
from tools.first_entry_v71.clock import AdmissionError,timestamp,anchor_rows,object_hash
from tools.first_entry_v7.selector import frame_from_v6,filter_frames
from tools.historical_strategy.analyze import extract,match_controls


def serializable(value):
    return json.loads(json.dumps(value,default=str,allow_nan=False))


def select(context,output):
    reg=read(ROOT/'tools/first_entry_v7/registration.json')
    rules=read(ROOT/'config/strategy.json')['rules'];fees=read(ROOT/'config/fees.json')['products']
    start,end=timestamp(context['lock']['first_scoring_eob']),timestamp(context['lock']['last_scoring_eob'])
    frames=[];opportunities=[];controls=[]
    for (cs,symbol),traces in sorted(context['traces'].items()):
        task=dict(csymbol=cs,symbol=symbol)
        tick=fees[cs.split('.')[1]]['price_tick']
        ops,ordinary,_,_=extract(task,traces,tick,rules)
        opportunities.extend(r for r in ops if start<=r['time']<=end)
        controls.extend(r for r in ordinary if start<=r['time']<=end)
        ordinals={timestamp(r['eob']):i for i,r in enumerate(context['anchors'][cs])}
        previous=None;previous_state=None;episode=0
        for index,row in enumerate(traces):
            now=timestamp(row['decision_eob']);state=row['groups']['S0']['trend_decision']['state']['trend_state']
            episode+=state!=previous_state
            contiguous=previous is None or ordinals[now]-ordinals[previous]==1
            frames.append(frame_from_v6(task,row,index,tick,rules,in_scoring_window=start<=now<=end,
                contiguous=contiguous,v6_episode=episode))
            previous,previous_state=now,state
    decisions,episodes=filter_frames(frames)
    for episode in episodes:
        marker='' if episode['start_known'] else 'LEFT_CENSORED@'
        episode['episode_id']='|'.join((episode['csymbol'],episode['actual_contract'],episode['direction'],marker+episode['start_eob']))
    decision_index={}
    for row in decisions:
        if row['reason'] in ('KEEP','PRE_SAMPLE_CONSUMED','EPISODE_SLOT_ALREADY_CONSUMED','LEFT_CENSORED_FIRST_UNKNOWN'):
            decision_index.setdefault(row['opportunity_id'],row)
    opportunities.sort(key=lambda r:(r['time'],r['csymbol'],r['actual_contract'],r['opportunity_id']))
    for row in opportunities:
        decision=decision_index[row['opportunity_id']]
        row.update(first_selected=decision['kept'],episode_id=decision['episode_id'],selection_reason=decision['reason'])
    fixed_pairs=match_controls(opportunities,controls)  # All S0 once; no outcome exists yet.
    payloads=dict(opportunities=opportunities,controls=controls,fixed_control_pairs=fixed_pairs,
                  decisions=decisions,episodes=episodes,coverage=context['coverage'])
    output.mkdir(parents=True,exist_ok=False)
    selection_dir=output/'selection';selection_dir.mkdir()
    hashes={}
    for name,value in payloads.items():
        value=serializable(value)
        write(selection_dir/(name+'.json'),value)
        hashes[name]=object_hash(value)
    receipt=dict(status='SELECTED_NOT_SCORED' if opportunities else 'SELECTED_NO_OPPORTUNITIES',
        purpose=context['lock']['purpose'],selection_object_sha256=hashes,
        sample_lock_sha256=object_hash(context['lock']),s0_opportunities=len(opportunities),
        first_selected=sum(r['first_selected'] for r in opportunities),fixed_matches=len(fixed_pairs),
        expected_m15=sum(r['expected_m15'] for r in context['coverage']),
        qualified_m15=sum(r['qualified_frames'] for r in context['coverage']),no_orders=True,
        future_labels_read=False)
    write(selection_dir/'receipt.json',receipt)
    return receipt


def run(manifest_path,input_root,output,sample_lock=DEFAULT_LOCK,fixture=False,score=False):
    if output.exists():
        raise AdmissionError('OUTPUT_ALREADY_EXISTS_USE_NEW_AUDITED_RUN')
    context=admit(manifest_path,input_root,sample_lock,fixture)
    receipt=select(context,output)
    if score:
        from tools.first_entry_v71.scoring import score_once
        summary=score_once(context,output)
        return dict(receipt,status='SCORED',decision=summary['verdict']['decision'])
    return receipt


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--input-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--sample-lock',type=Path,default=DEFAULT_LOCK)
    p.add_argument('--fixture',action='store_true')
    p.add_argument('--score',action='store_true')
    a=p.parse_args()
    try:
        receipt=run(a.manifest,a.input_root,a.output,a.sample_lock,a.fixture,a.score)
        print(receipt['status'],receipt['qualified_m15'],flush=True)
    except (AdmissionError,KeyError,ValueError,OSError) as exc:
        # A failed admission has diagnostics only, never a selection/score success receipt.
        path=a.output.parent/(a.output.name+'_failure.json')
        if not path.exists():
            path.parent.mkdir(parents=True,exist_ok=True)
            coverage=[]
            try:
                lock=read(a.sample_lock)
                for cs,product in lock.get('products',{}).items():
                    detail=getattr(exc,'detail',{})
                    affected=detail.get('csymbol')==cs
                    coverage.append(dict(csymbol=cs,expected_m15=product['expected_scoring_m15'],
                        actual_frames=detail.get('received',detail.get('actual_frames')) if affected else None,
                        qualified_frames=detail.get('qualified_frames') if affected else None,
                        state='FAILED_AT_REPORTED_STAGE' if affected else 'NOT_ASSESSED_OR_NOT_COMPLETED'))
            except (OSError,KeyError,ValueError):pass
            write(path,dict(status='READY_FOR_DATA' if 'READY_FOR_DATA' in str(exc) or getattr(exc,'code','')=='SAMPLE_LOCK_NOT_FROZEN' else 'DATA_NOT_QUALIFIED',
                code=getattr(exc,'code',type(exc).__name__),detail=getattr(exc,'detail',{}),
                coverage_by_product=coverage,success_receipt_written=False,no_orders=True))
        print('FAILED',getattr(exc,'code',type(exc).__name__),flush=True)
        raise SystemExit(2)


if __name__=='__main__':main()
