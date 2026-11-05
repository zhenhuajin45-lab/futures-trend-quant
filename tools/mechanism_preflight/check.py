"""Single source-defined M1 feasibility pass; outcomes have no callable entry."""
from pathlib import Path
import argparse,json,time
import pandas as pd
from .common import ROOT,read,write,sha,publish,mde_budget,output_target
from .features import read_prefixes,session_observation
from .routing import decide,by_stage,LEVELS
from .capacity import crosscheck


def run(inventory,mechanism,out,mode='features_only'):
    if mode!='features_only':raise ValueError('only features_only is authorized')
    output_target(out)
    inv=Path(inventory);inv=inv/'inventory.csv' if inv.is_dir() else inv
    binding=read(inv.parent/'index_binding.json')
    if sha(inv)!=binding['inventory_sha256']:raise ValueError('inventory digest mismatch')
    index=read(binding['source_index']);policy=read(binding['protected']);m=read(mechanism)
    if sha(binding['source_index'])!=binding['source_index_sha256'] or sha(binding['protected'])!=binding['protected_sha256']:raise ValueError('source index or protection policy changed')
    if m['selected_direction']!='M1_SESSION_RELATION' or m['universe']!=['DCE.M']:raise ValueError('unregistered mechanism/universe')
    if m['feature_only_definition']['past_window']!=['09:00','09:30'] or m['feature_only_definition']['signal_ready']!='14:30' or m['feature_only_definition']['expiry']!='14:55':raise ValueError('unregistered feature window')
    level=m.get('research_level')
    if level not in LEVELS:raise ValueError('unknown research level')
    stage_evidence=m.get('stage_evidence',{})
    if any(value is not None and type(value) is not bool for value in stage_evidence.values()):
        raise ValueError('stage evidence must be boolean or unknown null')
    frame=pd.read_csv(inv);sources={s['source_id']:s for s in index['sources']}
    # Calendar/mapping bodies contain no price or outcome fields and are separately bound.
    calendar_item=sources[index['calendar_source_id']];mapping_item=sources[index['mapping_source_id']]
    for item in (calendar_item,mapping_item):
        row=frame.loc[frame.source_id==item['source_id']].iloc[0]
        if sha(item['path'])!=row.sha256:raise ValueError('calendar/mapping changed')
    records=read(calendar_item['path']);selection=read(mapping_item['path'])
    from tools.full_trade_v8.prepare import corrected_schedule
    times,days,_=corrected_schedule(records)
    calendar={day:times[[d==day for d in days]] for day in sorted(set(days))}
    observation_days=[d for d in calendar if '2026-05-07'<=d<='2026-11-02']
    # Only the raw sources explicitly selected by provenance, never trade ledgers.
    bars=[];projection=[]
    for sid in index['feature_source_ids']:
        row=frame.loc[frame.source_id==sid].iloc[0].to_dict()
        if not bool(row['engineering_price_access']):raise ValueError('required M1 body blocked by inventory protection')
        a=read_prefixes(row,policy,observation_days);bars.append(a)
        projection.append(dict(source_id=sid,rows_read=len(a),price_columns=['open','high','low','close'],time_predicate='ONLY_09:01_TO_09:30_AND_14:30; NO_ENTRY_OR_EXIT_PRICE'))
    data=pd.concat(bars,ignore_index=True) if bars else pd.DataFrame(columns=['symbol','eob','open','high','low','close','volume'])
    observations=[];timings=[]
    for day in observation_days:
        symbol=selection.get(day)
        if symbol is None:
            observations.append(dict(day=day,symbol='UNKNOWN',feature_complete=False,issues=['MAPPING_UNAVAILABLE'],raw_opportunities=1));continue
        before=time.perf_counter_ns();r=session_observation(data,calendar[day],symbol,day)
        timings.append((time.perf_counter_ns()-before)/1e6);r['raw_opportunities']=1;observations.append(r)
    c,cross,risk_distribution=crosscheck(observations)
    n=int(c.nonoverlap_events.sum());risk=int(c.common_candidate_control_risk_events.sum())
    informative=int(c.informative_opposite_common_risk_events.sum())
    opposite_direction=int(c.informative_opposite_direction_events.sum())
    blocks={}
    for length in (5,10):
        full=c.iloc[:len(c)//length*length].copy();full['block_id']=[i//length for i in range(len(full))]
        grouped=full.groupby('block_id')[['nonoverlap_events','common_candidate_control_risk_events']].sum()
        blocks[str(length)]=dict(full_nonoverlapping_calendar_blocks=len(grouped),blocks_with_events=int((grouped.nonoverlap_events>0).sum()),
            blocks_with_common_risk=int((grouped.common_candidate_control_risk_events>0).sum()),tail_days=len(c)%length,independence_verified=False)
    routing_evidence=dict(mechanism_complete=all(bool(m.get(k)) for k in ('observable_trigger','expected_window','failure_exit','risk_anchor')),
        primary_mechanism_evidence=bool(m.get('evidence',{}).get('primary')),control_defined=bool(m.get('primary_control')),
        scope_incompatible=bool(len(c) and (c.window_status!='UNKNOWN').any() and not c.window_compatible.any()),capacity_proven_insufficient=not bool(n),required_feature_fields=bool(n),
        new_development_window=m['data_boundaries']['new_development_window_qualified'],
        separate_independent_window=m['data_boundaries']['new_independent_window_qualified'],
        delta_min_registered=m['delta_min_r'] is not None,level=level)
    # Evidence remains unknown until recorded in the bound configuration, never silently passed.
    for key in ('gross_protocol_frozen','independent_reservation_plan','gross_development_passed',
                'entry_exit_quotes','arrival_and_delay','fees_certified','capacity_design_complete',
                'candidate_frozen','independent_protocol_frozen','net_development_passed'):
        routing_evidence[key]=stage_evidence.get(key)
    gates=by_stage(routing_evidence)
    route=decide(routing_evidence);status=route['status'];reasons=route['reasons']
    feasibility=dict(kind='FEATURES_ONLY_NO_ECONOMIC_RESEARCH',selected_direction=m['selected_direction'],universe=['DCE.M'],
        raw_opportunities=len(c),feature_complete_days=int(c.complete_past_features.sum()),research_level=level,nonzero_events=int(c.raw_nonzero_direction_events.sum()),implementable_events=n,
        common_risk_support=risk,opposite_direction_support=opposite_direction,opposite_common_risk_support=informative,missing_days=int((c.complete_past_features==0).sum()),
        five_clocks=dict(event='LATEST_REQUIRED_M1_EVENT_14:30; DIRECTION_EVIDENCE_09:30',received='UNKNOWN',signal_ready='14:30_EVENT_TIME_PROXY',order_eligible='14:30_NOMINAL_PLUS_UNKNOWN_REAL_DELAY',fill_model='NOT_COMPUTED'),
        engineering_calendar_days=len(c),unique_trading_day_blocks=int(c.loc[c.nonoverlap_events>0,'trading_date'].nunique()),
        independent_pair_count='UNKNOWN; RAW_EVENTS_AND_DAYS_ARE_UPPER_BOUNDS_NOT_EFFECTIVE_PAIRS',
        calendar_block_capacity=blocks,
        adapted_H=24,source_paper_window_minutes=30,adaptation='NEW_HYPOTHESIS_OMITS_CLOSE_BUFFER_AND_STRICT_NEXT_BAR_WAIT_NOT_A_LITERAL_REPLICATION',
        projection=projection,processing_only_ms=dict(median=float(pd.Series(timings).median()),max=float(max(timings))),
        transport_or_exchange_latency_inferred=False,profit_or_target_labels_computed=False,
        continuous_obi5_verified=False,sparse_quote_pkls_deserialized=False,
        mechanism_sha256=sha(mechanism),inventory_sha256=sha(inv),protected_sha256=binding['protected_sha256'])
    decision=dict(status=status,reasons=reasons,selected_direction='M1_SESSION_RELATION_FOR_FEASIBILITY_ONLY',
        routing_evidence=routing_evidence,routing=route,gates_by_stage=gates,
        independent_economic_evidence=False,new_strategy_number_assigned=False,
        development_backtest_authorized=False,independent_test_consumed=False,orders=False,
        raw_data_requests_made=False,paid_resources_authorized=False,review_mode='SINGLE_AGENT_SELF_CHECK',
        completed=['SOURCE_INVENTORY','PAST_FEATURE_CAPACITY','DATA_BOUNDARY_SYNTHETIC_CHECKS'],
        second_arithmetic='SEPARATE_LOCAL_POSTCHECK_REQUIRED_NOT_CLAIMED_BY_THIS_ENTRY',
        in_progress=[],pending=['OWNER_DATA_SCOPE_DECISION','DELTA_MIN_ONLY_IF_FURTHER_RESEARCH_APPROVED','DEVELOPMENT_PROTOCOL_IF_QUALIFIED'],
        next_owner='PROJECT_OWNER; DATA_REQUIREMENTS_REPORT_ONLY_NO_AUTOMATIC_SUBSCRIPTION',
        old_research_capacity_not_new_validation=True)
    def build(stage):
        c.to_csv(stage/'capacity.csv',index=False)
        cross.to_csv(stage/'capacity_crosscheck.csv',index=False)
        risk_distribution.to_csv(stage/'risk_tick_distribution.csv',index=False)
        c.groupby('symbol')[['raw_opportunities','complete_past_features','raw_nonzero_direction_events',
            'nonoverlap_events','risk_valid_events','common_candidate_control_risk_events',
            'informative_opposite_direction_events','informative_opposite_common_risk_events']].sum().to_csv(stage/'capacity_by_leg.csv')
        write(stage/'gates_by_stage.json',dict(configured_level=level,mechanism_sha256=sha(mechanism),gates=gates))
        budgets=[]
        for label,count in [('RAW_FEATURE_EVENT_UPPER_BOUND',n),('COMMON_RISK_SUPPORT_UPPER_BOUND',risk),
                            ('OPPOSITE_DIRECTION_UPPER_BOUND',opposite_direction),('OPPOSITE_COMMON_RISK_UPPER_BOUND',informative)]:
            budgets.extend(dict(count_scope=label,count_is_not_measured_effective_sample_size=True,**row) for row in mde_budget(count))
        pd.DataFrame(budgets).to_csv(stage/'capacity_budget.csv',index=False)
        write(stage/'feasibility.json',feasibility);write(stage/'decision.json',decision)
        # Restricted explanatory cases contain no future response or prices.
        first=next((r for r in observations if r['feature_complete']),None);roll=None;previous=None
        for r in observations:
            symbol=r['symbol']
            if previous and symbol!='UNKNOWN' and symbol!=previous and r['feature_complete']:
                roll=r;break
            if symbol!='UNKNOWN':previous=symbol
        write(stage/'cases.local.json',dict(selection='FIRST_COMPLETE_DAY_AND_FIRST_MAPPING_ROLL_DAY; NOT_BY_OUTCOME',
            cases=[dict(kind=kind,metadata=row,status='AVAILABLE' if row else 'CASE_TYPE_ABSENT') for kind,row in [('FIRST_COMPLETE',first),('FIRST_ROLL',roll)]],
            price_values_published=False))
        write(stage/'observations.local.json',dict(feature_metadata=observations,no_future_response_fields=True))
    publish(out,build)
    return dict(status=status,raw_opportunities=len(c),feature_complete_days=feasibility['feature_complete_days'],nonzero_events=feasibility['nonzero_events'],implementable_events=n,common_risk_support=risk,opposite_common_risk_support=informative)


def main():
    p=argparse.ArgumentParser();p.add_argument('--inventory',type=Path,required=True);p.add_argument('--mechanism',type=Path,required=True);p.add_argument('--mode',default='features_only');p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();print(json.dumps(run(a.inventory,a.mechanism,a.out,a.mode)))


if __name__=='__main__':main()
