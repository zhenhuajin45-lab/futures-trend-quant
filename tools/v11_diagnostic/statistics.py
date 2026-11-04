"""Fixed descriptive partitions and equal-product/equal-time-layer comparisons."""
import math
from collections import Counter
import numpy as np
import pandas as pd

PATHS=('TARGET','STOP','AMBIGUOUS','MISSING','NEITHER')
SORTABLE=('TARGET','STOP','NEITHER')
THRESHOLDS=dict(unknown_fraction_max=.2,tertile_end_min=100,nine_cell_min=20,common_products_min=5,
    layer_each_side_min=5,matched_side_product_fraction_max=.5,unordered_path_fraction_max=.2,
    positive_product_fraction_min=[2,3],fixed_space_ratio=2,trim_fraction_each_side=.1)


def finite(value):
    return None if value is None or not np.isfinite(value) else float(value)


def trimmed(values):
    x=np.sort(np.asarray(values,dtype=float));k=len(x)//10
    return finite(x[k:len(x)-k].mean()) if len(x) else None


def partition(values,k):
    cuts=values.quantile(np.arange(1,k)/k,interpolation='linear').tolist() if len(values) else []
    return np.searchsorted(cuts,values,side='left')+1,cuts


def path_counts(f):
    counts={p:int((f.price_path==p).sum()) for p in PATHS};n=len(f)
    sortable=sum(counts[p] for p in SORTABLE)
    return dict(n=n,sortable=sortable,ambiguous=counts['AMBIGUOUS'],missing=counts['MISSING'],
        unknown_plus_double=counts['AMBIGUOUS']+counts['MISSING'],
        target_sortable=counts['TARGET']/sortable if sortable else None,
        stop_sortable=counts['STOP']/sortable if sortable else None,
        **{'path_'+p.lower()+'_n':counts[p] for p in PATHS},
        **{'path_'+p.lower()+'_share_all':counts[p]/n if n else None for p in PATHS})


def stats(f):
    r=f.gross_r.to_numpy(dtype=float);n=len(r);absolute=float(abs(r).sum());positive=r[r>0]
    by=f.csymbol.value_counts();maximum=int(by.max()) if n else 0
    tops=sorted(by[by==maximum].index.tolist()) if n else []
    return dict(trade_dates=int(f.entry_day.nunique()),products=int(f.csymbol.nunique()),
        mean_gross_r=finite(r.mean()) if n else None,median_gross_r=finite(np.median(r)) if n else None,
        p25=finite(np.quantile(r,.25)) if n else None,p75=finite(np.quantile(r,.75)) if n else None,
        trimmed_mean_gross_r=trimmed(r),mean_hold_minutes=finite(f.hold_minutes.mean()) if n else None,
        negative_share=float((r<0).mean()) if n else None,
        max_absolute_contribution=float(abs(r).max()/absolute) if absolute else None,
        max_profit_contribution=float(positive.max()/positive.sum()) if len(positive) else None,
        max_product_share=maximum/n if n else None,max_products='|'.join(tops),
        **path_counts(f))


def compare(f,scheme,time_col,low_mask,high_mask):
    rows=[];matched_low=[];matched_high=[]
    for (product,time),layer in f.groupby(['csymbol',time_col],sort=True):
        low=layer.loc[low_mask.loc[layer.index]];high=layer.loc[high_mask.loc[layer.index]]
        qualified=len(low)>=5 and len(high)>=5
        a=stats(low);b=stats(high)
        row=dict(scheme=scheme,csymbol=product,time_layer=int(time),eligible=qualified,
            reason='INCLUDED' if qualified else 'FEWER_THAN_5_ON_EITHER_SIDE',n_low=len(low),n_high=len(high))
        for name in ('mean_gross_r','median_gross_r','trimmed_mean_gross_r','target_sortable','stop_sortable'):
            row['low_'+name]=a[name];row['high_'+name]=b[name]
            row['delta_'+name]=b[name]-a[name] if a[name] is not None and b[name] is not None else None
        row.update(low_sortable=a['sortable'],high_sortable=b['sortable'])
        rows.append(row)
        if qualified:matched_low.extend(low.index.tolist());matched_high.extend(high.index.tolist())
    all_rows=pd.DataFrame(rows)
    included=all_rows.loc[all_rows.eligible].copy() if len(all_rows) else all_rows
    columns=['delta_'+n for n in ('mean_gross_r','median_gross_r','trimmed_mean_gross_r','target_sortable','stop_sortable')]
    # Never silently drop a qualified layer with no sortable paths.
    undefined_paths=bool(len(included) and ((included.low_sortable==0)|(included.high_sortable==0)).any())
    products=included.groupby('csymbol')[columns].mean() if len(included) else pd.DataFrame(columns=columns)
    product_rows=[]
    for cs,row in products.iterrows():
        product_rows.append(dict(scheme=scheme,csymbol=cs,time_layer='ALL_EQUAL_TIME_LAYERS',eligible=True,
            reason='EQUAL_LAYER_PRODUCT_ESTIMATE',**{k:finite(v) for k,v in row.items()}))
    left=f.loc[matched_low];right=f.loc[matched_high];ls=stats(left);hs=stats(right)
    all_low=f.loc[low_mask];all_high=f.loc[high_mask]
    result=dict(scheme=scheme,eligible_products=len(products),eligible_layers=len(included),
        excluded_layers=len(all_rows)-len(included),matched_low=len(left),matched_high=len(right),
        excluded_low=int(low_mask.sum())-len(left),excluded_high=int(high_mask.sum())-len(right),
        positive_products=int((products.delta_mean_gross_r>0).sum()) if len(products) else 0,
        undefined_paths=undefined_paths,low=ls,high=hs,
        pooled_all_difference=finite(all_high.gross_r.mean()-all_low.gross_r.mean()),
        pooled_matched_difference=finite(right.gross_r.mean()-left.gross_r.mean()),
        differences={k.removeprefix('delta_'):finite(products[k].mean()) for k in columns})
    if undefined_paths:
        result['differences']['target_sortable']=None;result['differences']['stop_sortable']=None
    counts=pd.concat([left.csymbol,right.csymbol]).value_counts()
    tops=sorted(counts[counts==counts.max()].index.tolist()) if len(counts) else []
    result['leave_top_combined_product_out']={cs:finite(products.drop(index=cs).delta_mean_gross_r.mean()) for cs in tops}
    result['top_combined_products']=tops
    # Exact product counts for each matched side; concentration is not pooled.
    result['matched_side_product_counts']={name:{str(k):int(v) for k,v in side.csymbol.value_counts().items()} for name,side in [('low',left),('high',right)]}
    return rows+product_rows,result,left,right


def analyze(frame,prereg):
    if prereg.get('thresholds',THRESHOLDS)!=THRESHOLDS:raise ValueError('registered thresholds differ from frozen implementation')
    f=frame.copy().reset_index(drop=True)
    if f.trade_id.duplicated().any():raise ValueError('duplicate trade ID')
    if len(f)!=prereg['cohort']['closed']:raise ValueError('cohort count mismatch')
    if not np.isfinite(f.gross_r).all():raise ValueError('undefined ledger R')
    for column in ('range_ratio','remaining_minutes','hold_minutes'):
        f[column]=pd.to_numeric(f[column],errors='raise')
        if np.isinf(f[column]).any() or (f[column].dropna()<0).any():raise ValueError('invalid feature value')
    invalid=~f.price_path.isin([*PATHS,'NOT_COMPUTABLE'])
    if invalid.any():raise ValueError('unknown path code')
    path_not_computable=int((f.price_path=='NOT_COMPUTABLE').sum())
    f['price_path']=f.price_path.replace({'NOT_COMPUTABLE':'MISSING'})
    common=f.dropna(subset=['remaining_minutes','range_ratio']).copy()
    cells=[];composition=[];paths=[];within=[];comparisons={};cuts={};corners={};matched={}
    for k in (3,2):
        common['t'+str(k)],ct=partition(common.remaining_minutes,k)
        common['s'+str(k)],cs=partition(common.range_ratio,k)
        cuts[str(k)]=dict(time=ct,space=cs)
        for t in range(1,k+1):
            for s in range(1,k+1):
                cell=common[(common['t'+str(k)]==t)&(common['s'+str(k)]==s)]
                row=dict(scheme=str(k)+'x'+str(k),time_layer=t,space_layer=s,**stats(cell));cells.append(row)
                paths.append(dict(scheme=row['scheme'],time_layer=t,space_layer=s,**path_counts(cell)))
                tops=row['max_products'].split('|') if len(cell) else []
                for product,group in cell.groupby('csymbol'):
                    composition.append(dict(scheme=row['scheme'],time_layer=t,space_layer=s,csymbol=product,
                        n=len(group),weight=len(group)/len(cell),mean_gross_r=float(group.gross_r.mean()),
                        tied_largest=product in tops,mean_without_this_product=finite(cell.loc[cell.csymbol!=product,'gross_r'].mean())))
        scheme='tertiles' if k==3 else 'median'
        rows,result,lo,hi=compare(common,scheme,'t'+str(k),common['s'+str(k)]==1,common['s'+str(k)]==k)
        within.extend(rows);comparisons[scheme]=result;matched[scheme]=(lo,hi)
        corner_low=common[(common['t'+str(k)]==1)&(common['s'+str(k)]==1)]
        corner_high=common[(common['t'+str(k)]==k)&(common['s'+str(k)]==k)]
        corners[str(k)]=dict(low=stats(corner_low),high=stats(corner_high),high_minus_low=finite(corner_high.gross_r.mean()-corner_low.gross_r.mean()))
    rows,result,lo,hi=compare(common,'fixed_2r','t3',common.range_ratio<2,common.range_ratio>=2)
    within.extend(rows);comparisons['fixed_2r']=result;matched['fixed_2r']=(lo,hi)
    fixed=[]
    for t in range(1,4):
        for name,mask in [('LT_2',common.range_ratio<2),('GE_2',common.range_ratio>=2)]:
            group=common.loc[(common.t3==t)&mask]
            fixed.append(dict(time_layer=t,space_group=name,**stats(group)))
    unknown=f.loc[~f.index.isin(common.index)]
    # Fixed 2R table preserves every mother-cohort row including unknown time/space.
    for name,group in [('LT_2_ALL',f.loc[f.range_ratio<2]),('GE_2_ALL',f.loc[f.range_ratio>=2]),('UNKNOWN_SPACE',f.loc[f.range_ratio.isna()])]:
        fixed.append(dict(time_layer='ALL',space_group=name,**stats(group)))
    for name,group in f.loc[f.range_ratio.isna()].groupby('space_status',dropna=False):
        fixed.append(dict(time_layer='UNKNOWN',space_group='UNKNOWN_REASON:'+str(name),**stats(group)))
    fixed.extend(dict(row_type='WITHIN_PRODUCT_DELTA',space_group='GE_2_MINUS_LT_2',**r) for r in rows)
    fixed.append(dict(row_type='EQUAL_PRODUCT_DELTA',time_layer='ALL_QUALIFIED_EQUAL_LAYERS',space_group='GE_2_MINUS_LT_2',
        eligible_products=result['eligible_products'],matched_low=result['matched_low'],matched_high=result['matched_high'],
        **{'delta_'+k:v for k,v in result['differences'].items()}))
    for scheme,(low,high) in matched.items():
        for name,g in [('LOW',low),('HIGH',high)]:paths.append(dict(scheme=scheme,time_layer='MATCHED_ALL',space_layer=name,**path_counts(g)))
    coverage=dict(closed=len(f),open_excluded=prereg['cohort']['open'],unfilled='NOT_IN_EXECUTED_LEDGER; NO_OPPORTUNITY_CLAIM',
        known_time=int(f.remaining_minutes.notna().sum()),known_space=int(f.range_ratio.notna().sum()),common=len(common),
        unknown_both_dimensions=len(f)-len(common),unknown_ratio=(len(f)-len(common))/len(f),
        missing_reasons={str(k):int(v) for k,v in unknown.space_status.value_counts(dropna=False).items()},
        missing_time_reasons=dict(deadline_unknown=int(f.remaining_minutes.isna().sum())),
        source_path_not_computable=path_not_computable,cuts=cuts,
        distribution_definition='SAMPLE_QUANTILES_LINEAR; TRIM_FLOOR_N_DIV_10_PER_SIDE; NO_UNDEFINED_CONTRIBUTION_AS_ZERO')
    reasons={kind:[] for kind in ('INDETERMINATE','STOP_PRODUCT_MIX','STOP_MECHANISM')}
    def fail(kind,code,**details):reasons[kind].append(dict(code=code,**details))
    if (len(f)-len(common))*5>len(f):fail('INDETERMINATE','UNKNOWN_DIMENSIONS_GT_20PCT',unknown=len(f)-len(common),denominator=len(f))
    if len(common):
        for side,mask in [('low',common.s3==1),('high',common.s3==3)]:
            if int(mask.sum())<100:fail('INDETERMINATE','TERTILE_END_LT_100',side=side,n=int(mask.sum()))
    else:fail('INDETERMINATE','NO_COMMON_SAMPLE')
    sparse=[dict(time=r['time_layer'],space=r['space_layer'],n=r['n']) for r in cells if r['scheme']=='3x3' and r['n']<20]
    if sparse:fail('INDETERMINATE','NINE_CELL_LT_20',cells=sparse)
    for scheme,c in comparisons.items():
        if c['eligible_products']<5:fail('INDETERMINATE','FEWER_THAN_5_COMMON_PRODUCTS',scheme=scheme,n=c['eligible_products'])
        if c['undefined_paths']:fail('INDETERMINATE','QUALIFIED_LAYER_WITHOUT_SORTABLE_PATH',scheme=scheme)
        for side in ('low','high'):
            a=c[side]
            if a['unknown_plus_double']*5>a['n']:fail('INDETERMINATE','UNORDERED_PATH_GT_20PCT',scheme=scheme,side=side,n=a['unknown_plus_double'],denominator=a['n'])
        if any(v is None for v in c['differences'].values()):fail('INDETERMINATE','REQUIRED_STAT_UNDEFINED',scheme=scheme)
    c=comparisons['tertiles'];d=c['differences']['mean_gross_r']
    if c['pooled_all_difference'] is not None and c['pooled_all_difference']>0 and d is not None and d<=0:
        fail('STOP_PRODUCT_MIX','POOLED_WITHIN_DIRECTION_REVERSAL',pooled=c['pooled_all_difference'],within=d)
    for side in ('low','high'):
        counts=c['matched_side_product_counts'][side];n=sum(counts.values())
        for product,count in counts.items():
            if count*2>n:fail('STOP_PRODUCT_MIX','MATCHED_SIDE_PRODUCT_GT_50PCT',side=side,csymbol=product,n=count,denominator=n)
    if d is not None and d>0:
        for product,delta in c['leave_top_combined_product_out'].items():
            if delta is None or delta<=0:fail('STOP_PRODUCT_MIX','DEPENDENT_ON_TIED_TOP_PRODUCT',csymbol=product,without=delta)
    for scheme in ('tertiles','median','fixed_2r'):
        a=comparisons[scheme]['differences'];mean=a['mean_gross_r']
        if mean is not None and mean<=0:fail('STOP_MECHANISM','FIXED_2R_NO_SEPARATION' if scheme=='fixed_2r' else 'MEAN_NOT_POSITIVE',scheme=scheme,value=mean)
        target=a['target_sortable'];stop=a['stop_sortable']
        if scheme in ('tertiles','fixed_2r'):
            if (target is not None and target<0) or (stop is not None and stop>0):fail('STOP_MECHANISM','PATH_CONFLICT',scheme=scheme,target_delta=target,stop_delta=stop)
            if scheme=='tertiles' and target==0 and stop==0:fail('STOP_MECHANISM','NO_STRICT_PATH_IMPROVEMENT')
    if c['positive_products']*3<c['eligible_products']*2:fail('STOP_MECHANISM','FEWER_THAN_TWO_THIRDS_PRODUCTS_POSITIVE',positive=c['positive_products'],denominator=c['eligible_products'])
    for name in ('median_gross_r','trimmed_mean_gross_r'):
        v=c['differences'][name]
        if v is not None and v<=0:fail('STOP_MECHANISM','DISTRIBUTION_UNSTABLE',metric=name,value=v)
    status=next((s for s in reasons if reasons[s]),'READY_FOR_BLIND_REVIEW')
    repairable=any(x['code'] in ('UNKNOWN_DIMENSIONS_GT_20PCT','UNORDERED_PATH_GT_20PCT','REQUIRED_STAT_UNDEFINED','QUALIFIED_LAYER_WITHOUT_SORTABLE_PATH') for x in reasons['INDETERMINATE'])
    decision=dict(status=status,reasons=reasons,comparisons=comparisons,corner_diagnostics=corners,
        decision_priority=['INDETERMINATE','STOP_PRODUCT_MIX','STOP_MECHANISM','READY_FOR_BLIND_REVIEW'],
        repair_policy='ONE_DOCUMENTED_SAME_BYTES_FIX_ONLY' if status=='INDETERMINATE' and repairable else 'ARCHIVE_LOW_CAPACITY_NO_EXTENSION' if status=='INDETERMINATE' else 'NO_REPAIR_OR_THRESHOLD_SEARCH',
        automatically_releases_new_sample=False,simulation_orders=False,real_orders=False,
        next_owner='NON_ANSWERING_HOST_AND_BLIND_REVIEWERS' if status=='READY_FOR_BLIND_REVIEW' else 'PROJECT_OWNER_STOP_OR_ARCHIVE')
    return dict(cells=pd.DataFrame(cells),cell_products=pd.DataFrame(composition),within_product=pd.DataFrame(within),
        fixed_2r=pd.DataFrame(fixed),path=pd.DataFrame(paths),coverage=coverage,decision=decision)
