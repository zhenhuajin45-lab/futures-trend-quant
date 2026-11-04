"""The only new-window score entry; outcomes are read after selection is fixed."""
from pathlib import Path
import json
import pandas as pd
from collections import defaultdict
from backtest.forward_v3 import read,write,digest
from tools.first_entry_v71.clock import AdmissionError,object_hash,timestamp
from tools.first_entry_v71.metrics import evaluate
from tools.first_entry_v71.admission import normalize_times
from tools.historical_strategy.price_paths import label_opportunity,ExpectedClock

ROOT=Path(__file__).resolve().parents[2]


def episode_boundary_diagnostics(row,episode,window_end):
    """Post-selection diagnostics; never truncate/reclassify the frozen 48-bar label."""
    end=timestamp(episode['end_eob']) if episode.get('end_known') and episode.get('end_eob') else None
    label=row['structural']
    row['label_window_end_eob']=window_end
    row['label_window_crosses_episode_end']=None if end is None or window_end is None else timestamp(window_end)>end
    entry=label.get('entry_bob');touch=label.get('first_touch_eob')
    row['entry_after_episode_end']=None if end is None or entry is None else timestamp(entry)>=end
    row['first_touch_after_episode_end']=None if end is None or touch is None else timestamp(touch)>end
    return row


def score_once(context,output):
    folder=output/'selection'
    receipt=read(folder/'receipt.json')
    if receipt['sample_lock_sha256']!=object_hash(context['lock']):
        raise AdmissionError('SELECTION_SAMPLE_LOCK_CHANGED')
    for name,expected in receipt['selection_object_sha256'].items():
        if object_hash(read(folder/(name+'.json')))!=expected:
            raise AdmissionError('SELECTION_KEYS_CHANGED_BEFORE_LABELS',file=name)
    score_dir=output/'score'
    if score_dir.exists():
        raise AdmissionError('SCORE_ALREADY_CLAIMED')
    if context['lock']['purpose']=='INDEPENDENT_RESEARCH':
        claims=ROOT/'data/cache/v71_score_claims';claims.mkdir(parents=True,exist_ok=True)
        study_key=context['lock'].get('registration_sha256',object_hash(context['lock']))
        claim=claims/(study_key+'.json')
        try:
            with claim.open('x',encoding='utf8') as stream:
                json.dump(dict(sample_lock_sha256=object_hash(context['lock']),selection_sha256=object_hash(receipt),
                               output=str(output.resolve()),status='CLAIMED_BEFORE_LABELS'),stream)
        except FileExistsError:
            raise AdmissionError('SAMPLE_ALREADY_SCORED_OR_CLAIMED')
    score_dir.mkdir(exist_ok=False)
    with (score_dir/'once_claim.json').open('x',encoding='utf8') as stream:
        json.dump(dict(sample_lock_sha256=object_hash(context['lock']),selection_sha256=object_hash(receipt),
                       purpose=context['lock']['purpose'],rule_variants=1),stream)
    opportunities=read(folder/'opportunities.json');pairs=read(folder/'fixed_control_pairs.json')
    episodes=read(folder/'episodes.json');controls={}
    episode_index={r['episode_id']:r for r in episodes}
    fees=read(ROOT/'config/fees.json')['products'];rules=read(ROOT/'config/strategy.json')['rules']
    by_leg=defaultdict(list)
    for opportunity in opportunities:by_leg[(opportunity['csymbol'],opportunity['actual_contract'])].append(opportunity)
    labeled_by_key={}
    # Index one used leg at a time, not every contract's full M1 as Python dicts.
    for leg,leg_opportunities in sorted(by_leg.items()):
        raw_path=context['raw_paths'][leg]
        if digest(raw_path)!=context['raw_hashes'][leg]:raise AdmissionError('RAW_SNAPSHOT_CHANGED_AFTER_SELECTION')
        frame=normalize_times(pd.read_parquet(raw_path))
        if digest(raw_path)!=context['raw_hashes'][leg]:raise AdmissionError('RAW_SNAPSHOT_CHANGED_DURING_LABEL_READ')
        records=frame.to_dict('records');expected=list(context['expected'][leg[0]])
        raw=dict(list=records,bobs=list(frame.bob),by_eob={r['eob']:r for r in records},clock_index=ExpectedClock(expected))
        for opportunity in leg_opportunities:
            row=dict(opportunity);signal=dict(row,time=timestamp(row['time']))
            tick=fees[row['csymbol'].split('.')[1]]['price_tick']
            row['structural']=label_opportunity(signal,raw,expected,tick,rules,48)
            row['symmetric']=label_opportunity(signal,raw,expected,tick,rules,48,risk_atr=1.5)
            window,_=raw['clock_index'].window(signal['time'],48)
            episode_boundary_diagnostics(row,episode_index[row['episode_id']],window[-1].isoformat() if window else None)
            key=row['opportunity_id']
            if key in pairs:
                control=dict(pairs[key],time=timestamp(pairs[key]['time']))
                controls[key]=label_opportunity(control,raw,expected,tick,rules,48,risk_atr=1.5)
            labeled_by_key[key]=row
        del records,raw,frame
    labeled=[labeled_by_key[r['opportunity_id']] for r in opportunities]
    days=sorted({day for p in context['lock']['products'].values() for day in p['scoring_trade_dates']})
    product_days=sum(len(p['scoring_trade_dates']) for p in context['lock']['products'].values())
    summary,episode_table,pair_table=evaluate(labeled,episodes,controls,days,product_days)
    summary.update(status='SCORED',purpose=context['lock']['purpose'],sample_lock_sha256=object_hash(context['lock']),
        selection_sha256=object_hash(receipt),fixed_pair_keys_sha256=object_hash(pairs),
        no_orders=True,no_net_returns=True,m5='STOPPED',new_strategy_variants=1)
    summary['episode_boundary_diagnostics']={group:{name:sum(r.get(name) is True for r in labeled if group=='all_s0' or r['first_selected'])
        for name in ('label_window_crosses_episode_end','entry_after_episode_end','first_touch_after_episode_end')}
        for group in ('all_s0','first')}
    summary['episode_boundary_diagnostics']['primary_policy']='48_BAR_STATIC_PRICE_PATH_UNCHANGED; CROSS_EPISODE_FLAGS_DO_NOT_FILTER_OR_IMPLY_REALIZED_TREND_EXIT_RETURNS'
    if context['lock']['purpose']=='ENGINEERING_FIXTURE_ONLY':
        summary['real_strategy_inference']='PROHIBITED; SYNTHETIC ENGINEERING ONLY'
    public=[]
    for row in labeled:
        item={key:row.get(key) for key in ('opportunity_id','csymbol','actual_contract','time','trading_date','direction',
                                          'episode_id','first_selected','selection_reason','evidence_id','path',
                                          'label_window_end_eob','label_window_crosses_episode_end',
                                          'entry_after_episode_end','first_touch_after_episode_end')}
        for mode in ('structural','symmetric'):
            item.update({mode+'_'+k:row[mode].get(k) for k in ('label','complete','r_over_atr','entry_eob','first_touch_eob')})
        public.append(item)
    opportunity_index={r['opportunity_id']:r for r in labeled}
    for pair in pair_table:
        signal=opportunity_index[pair['opportunity_id']]
        control=pairs.get(pair['opportunity_id'])
        pair['actual_contract']=signal['actual_contract']
        pair['signal_time']=signal['time']
        pair['control_time']=None if control is None else control['time']
        pair['control_key']=None if control is None else '|'.join(str(control[k]) for k in
            ('csymbol','actual_contract','direction','time','i'))
    # Three public derived tables; licensed prices are isolated in the local run.
    pd.DataFrame(public,columns=list(public[0]) if public else ['opportunity_id','first_selected','structural_label']).to_csv(score_dir/'opportunity_table.csv',index=False)
    pd.DataFrame(episode_table,columns=list(episode_table[0]) if episode_table else ['episode_id','eligible','first_captured']).to_csv(score_dir/'episode_table.csv',index=False)
    pd.DataFrame(pair_table,columns=list(pair_table[0]) if pair_table else ['opportunity_id','matched','both_complete']).to_csv(score_dir/'ordinary_pair_table.csv',index=False)
    write(score_dir/'summary.json',summary)
    write(score_dir/'private_labeled_opportunities.json',labeled)
    write(score_dir/'receipt.json',dict(status='SCORED',purpose=context['lock']['purpose'],
        summary_sha256=object_hash(summary),selection_immutable=True,no_orders=True))
    return summary
