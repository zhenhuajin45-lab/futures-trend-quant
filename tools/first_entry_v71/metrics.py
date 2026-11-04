"""Nested-sample and episode metrics. No fees, fills, holdings or net returns."""
from collections import Counter,defaultdict
import numpy as np
from tools.first_entry_v7.symmetric import comparison
from tools.historical_strategy.analyze import interval


def joint_interval(panel,days,length,ratio):
    if len(days)<length:return None
    values=np.array([panel.get(day,[0.]*len(next(iter(panel.values())))) for day in days],dtype=float) if panel else None
    if values is None:return None
    rng=np.random.default_rng(20261104)
    starts=rng.integers(0,len(days)-length+1,size=(10000,(len(days)+length-1)//length))
    indexes=(starts[:,:,None]+np.arange(length)).reshape(10000,-1)[:,:len(days)]
    results=ratio(values[indexes].sum(axis=1))
    results=results[np.isfinite(results)]
    if len(results)==0:return None
    return dict(block_days=length,replicates=len(results),low=float(np.quantile(results,.025)),
                high=float(np.quantile(results,.975)),median=float(np.median(results)))


def label_summary(rows):
    full=[r for r in rows if r['complete']]
    n=len(full);target=sum(r['label']=='TARGET_FIRST' for r in full);dual=sum(r['label']=='BOTH_SAME_M1' for r in full)
    return dict(all=len(rows),complete=n,target_first=target,target_rate=target/n if n else None,
        target_upper=(target+dual)/n if n else None,stop_first=sum(r['label']=='STOP_FIRST' for r in full),
        labels=dict(Counter(r['label'] for r in rows)),incomplete=sum(not r['complete'] for r in rows),
        censored=sum(r['label']=='CENSORED' for r in rows),invalid_at_open=sum(r['label']=='INVALID_AT_OPEN' for r in rows))


def nested_quality(rows,days):
    selected=[r for r in rows if r['first_selected']]
    panel=defaultdict(lambda:[0,0,0,0])
    for row in rows:
        label=row['structural']
        if not label['complete']:continue
        p=panel[row['trading_date']]
        p[2]+=label['label']=='TARGET_FIRST';p[3]+=1
        if row['first_selected']:
            p[0]+=label['label']=='TARGET_FIRST';p[1]+=1
    def ratio(v):
        with np.errstate(divide='ignore',invalid='ignore'):return v[:,0]/v[:,1]-v[:,2]/v[:,3]
    full=label_summary([r['structural'] for r in rows]);first=label_summary([r['structural'] for r in selected])
    difference=first['target_rate']-full['target_rate'] if first['target_rate'] is not None and full['target_rate'] is not None else None
    # Shared selected uncertainties have the same outcome in both nested arms.
    def known_and_unknown(group):
        return (sum(r['structural']['label']=='TARGET_FIRST' for r in group),
                sum(r['structural']['label'] in ('CENSORED','BOTH_SAME_M1') for r in group))
    removed=[r for r in rows if not r['first_selected']]
    sw,su=known_and_unknown(selected);rw,ru=known_and_unknown(removed)
    bounds=None
    if selected and rows:
        bounds=dict(lower=sw/len(selected)-(sw+rw+ru)/len(rows),
            upper=(sw+su)/len(selected)-(sw+su+rw)/len(rows),
            denominator_policy='ALL_SIGNAL_TIME_VALID_SLOTS; INVALID_OPEN_IS_FAILURE; KNOWN_TOUCH_RETAINED',
            selected_uncertain=su,removed_uncertain=ru)
    return dict(s0=full,first=first,difference=difference,block5=joint_interval(panel,days,5,ratio),
        block10=joint_interval(panel,days,10,ratio),all_slot_censor_bounds=bounds,
        removed=label_summary([r['structural'] for r in removed]),
        removed_target_events=sum(r['structural']['complete'] and r['structural']['label']=='TARGET_FIRST' for r in removed))


def episode_capacity(rows,episodes,days,product_days):
    groups=defaultdict(list)
    for row in rows:groups[row['episode_id']].append(row)
    metadata={r['episode_id']:r for r in episodes}
    table=[];panel=defaultdict(lambda:[0,0]);valuable=captured=0;boundary=potential_capture=0
    for key,opps in sorted(groups.items()):
        meta=metadata[key];kept=[r for r in opps if r['first_selected']]
        reasons=[]
        if not meta['start_known']:reasons.append('LEFT_CENSORED')
        if not meta['end_known']:reasons.append('OPEN_END')
        if meta['selected_key'] is not None and meta['selected_key'] not in {r['opportunity_id'] for r in opps}:
            reasons.append('PRE_SAMPLE_CONSUMED')
        uncertain=any(r['structural']['label'] in ('CENSORED','BOTH_SAME_M1','INVALID_PRICE_LEVEL') or
            (not r['structural']['complete'] and r['structural']['label'] not in ('INVALID_AT_OPEN',)) for r in opps)
        if uncertain:reasons.append('INCOMPLETE_OR_AMBIGUOUS_OPPORTUNITY')
        baseline_win=any(r['structural']['complete'] and r['structural']['label']=='TARGET_FIRST' for r in opps)
        first_win=any(r['structural']['complete'] and r['structural']['label']=='TARGET_FIRST' for r in kept)
        eligible=not reasons
        if eligible and baseline_win:
            valuable+=1;captured+=first_win
            panel[opps[0]['trading_date']][1]+=1;panel[opps[0]['trading_date']][0]+=first_win
        if not eligible:
            boundary+=1
            potential_capture+=any(r['structural']['label'] in ('TARGET_FIRST','BOTH_SAME_M1','CENSORED') for r in kept)
        table.append(dict(episode_id=key,csymbol=opps[0]['csymbol'],actual_contract=opps[0]['actual_contract'],
            trading_date=opps[0]['trading_date'],eligible=eligible,exclusion_reasons=reasons,s0_valuable=baseline_win,
            first_captured=first_win,s0_opportunities=len(opps),first_opportunities=len(kept),
            selected_key=meta['selected_key'],start_known=meta['start_known'],end_known=meta['end_known'],
            start_eob=meta.get('start_eob'),end_eob=meta.get('end_eob')))
    def ratio(v):
        with np.errstate(divide='ignore',invalid='ignore'):return v[:,0]/v[:,1]
    ratio_point=captured/valuable if valuable else None
    bounds=dict(lower=captured/(valuable+boundary) if valuable+boundary else None,
                upper=(captured+potential_capture)/(valuable+potential_capture) if valuable+potential_capture else None,
                boundary_episodes=boundary,potentially_captured_boundary_episodes=potential_capture)
    summary=dict(valuable_complete_episodes=valuable,captured_complete_episodes=captured,
        missed_complete_episodes=valuable-captured,retention=ratio_point,
        block5=joint_interval(panel,days,5,ratio),block10=joint_interval(panel,days,10,ratio),
        boundary_bounds=bounds,episodes_with_opportunities=len(groups),complete_episode_coverage=sum(r['eligible'] for r in table)/len(table) if table else None,
        exclusions=dict(Counter(reason for r in table for reason in r['exclusion_reasons'])),
        captured_per_100_product_days=100*captured/product_days if product_days else None,
        captured_products=sorted({r['csymbol'] for r in table if r['eligible'] and r['first_captured']}),
        captured_trade_dates=sorted({r['trading_date'] for r in table if r['eligible'] and r['first_captured']}))
    return summary,table


def ordinary_pairs(rows,control_labels,days):
    selected=[r for r in rows if r['first_selected']]
    matches=[r for r in selected if r['opportunity_id'] in control_labels]
    complete=[r for r in matches if r['symmetric']['complete'] and control_labels[r['opportunity_id']]['complete']]
    table=[dict(opportunity_id=r['opportunity_id'],csymbol=r['csymbol'],trading_date=r['trading_date'],
        first_selected=r['first_selected'],signal_label=r['symmetric']['label'],
        control_label=None if r['opportunity_id'] not in control_labels else control_labels[r['opportunity_id']]['label'],
        matched=r['opportunity_id'] in control_labels,
        both_complete=r['opportunity_id'] in control_labels and r['symmetric']['complete'] and control_labels[r['opportunity_id']]['complete']) for r in rows]
    result=comparison([(r['trading_date'],r['symmetric']['label'],control_labels[r['opportunity_id']]['label']) for r in complete],days) if complete else None
    lower,upper=defaultdict(lambda:[0,0]),defaultdict(lambda:[0,0])
    def win_bounds(label):
        return (1,1) if label['label']=='TARGET_FIRST' else (0,1) if label['label'] in ('CENSORED','BOTH_SAME_M1') else (0,0)
    for row in matches:
        a,b=win_bounds(row['symmetric']),win_bounds(control_labels[row['opportunity_id']])
        lower[row['trading_date']][0]+=a[0]-b[1];lower[row['trading_date']][1]+=1
        upper[row['trading_date']][0]+=a[1]-b[0];upper[row['trading_date']][1]+=1
    censor_bounds=dict(lower=sum(v[0] for v in lower.values())/len(matches),
        upper=sum(v[0] for v in upper.values())/len(matches),worst_block5=interval(lower,days,5),
        best_block5=interval(upper,days,5)) if matches else None
    return dict(selected=len(selected),matched=len(matches),complete_pairs=len(complete),
        match_coverage=len(matches)/len(selected) if selected else None,
        complete_pair_coverage=len(complete)/len(matches) if matches else None,comparison=result,
        all_matched_censor_bounds=censor_bounds),table


def verdict(quality,capacity,ordinary):
    q5,q10=quality['block5'],quality['block10'];diff=quality['difference']
    retention=capacity['retention'];bounds=capacity['boundary_bounds'];qb=quality['all_slot_censor_bounds']
    if diff is None or retention is None or q5 is None or q10 is None:
        return dict(decision='INSUFFICIENT_EVIDENCE',reason='NO_COMPLETE_DENOMINATOR_OR_BLOCK_PRECISION',promoted=False)
    intervals=[q5,q10,capacity['block5'],capacity['block10']]
    paired_intervals=ordinary.get('comparison')
    if paired_intervals is not None:
        intervals += [paired_intervals['target_difference']['block5'],paired_intervals['target_difference']['block10']]
    if any(interval is None or interval['replicates']!=10000 for interval in intervals):
        return dict(decision='INSUFFICIENT_EVIDENCE',reason='BLOCK_DENOMINATOR_MISSING_IN_SOME_RESAMPLES',promoted=False)
    censor_sensitive=(qb is not None and qb['lower']<.02<=qb['upper']) or (
        bounds['lower'] is not None and bounds['lower']<.5<=bounds['upper'])
    if censor_sensitive:
        return dict(decision='INSUFFICIENT_EVIDENCE',reason='CENSOR_BOUNDS_CHANGE_GATE',promoted=False)
    c5=capacity['block5']
    if (q5['high']<.02 and (qb is None or qb['upper']<.02)) or (bounds['upper'] is not None and bounds['upper']<.5 and c5 is not None and c5['high']<.5):
        return dict(decision='ARCHIVE_FILTER_ROUTE',reason='CLEAR_QUALITY_OR_EPISODE_CAPACITY_FAILURE',promoted=False)
    quality_pass = (diff >= .02 and q5['low'] > 0 and q10['median'] > 0
                    and qb is not None and qb['lower'] >= .02)
    capacity_pass=retention>=.5 and bounds['lower'] is not None and bounds['lower']>=.5
    paired=ordinary['comparison']
    ob=ordinary['all_matched_censor_bounds']
    ordinary_pass=ordinary['match_coverage'] is not None and ordinary['match_coverage']>=.7 and paired is not None and paired['ambiguous_lower']['block5'] is not None and paired['ambiguous_lower']['block5']['low']>0 and ob is not None and ob['worst_block5'] is not None and ob['worst_block5']['low']>0
    passed=quality_pass and capacity_pass and ordinary_pass
    return dict(decision='RETAIN_RESEARCH_CANDIDATE' if passed else 'INSUFFICIENT_EVIDENCE',
        promoted=passed,quality_pass=quality_pass,capacity_pass=capacity_pass,ordinary_pass=ordinary_pass,
        relative_improvement_only=quality_pass and capacity_pass and not ordinary_pass,
        reason='ALL_REGISTERED_RESEARCH_GATES_PASS' if passed else 'GATE_OR_PRECISION_INSUFFICIENT',
        live_execution_approved=False)


def evaluate(rows,episodes,control_labels,days,product_days):
    quality=nested_quality(rows,days)
    capacity,episode_table=episode_capacity(rows,episodes,days,product_days)
    ordinary,pair_table=ordinary_pairs(rows,control_labels,days)
    return dict(quality=quality,capacity=capacity,ordinary=ordinary,
        verdict=verdict(quality,capacity,ordinary)),episode_table,pair_table
