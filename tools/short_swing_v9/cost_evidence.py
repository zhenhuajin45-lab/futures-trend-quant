"""Fail-closed cost contract. No authentication, market requests or trade orders.

Fees remain separate from slippage: a 2-tick scenario means the matched dated
fee schedule PLUS 2 adverse ticks on each side, never 2 ticks all-inclusive.
Sparse representative quotes cannot certify uncovered ledger executions.
"""
from pathlib import Path
import math
import pandas as pd
from backtest.forward_v3 import read,digest

PENDING='ECONOMICS_PENDING_COST_VALIDATION'
KEYS=tuple(f'{offset}_{kind}' for offset in ('open','close','today') for kind in ('rate','per_lot'))


def pending(reason):
    return dict(status=PENDING,reason=reason,fee_model_matches=False,ledger_coverage_complete=False)


def normalized_fees(model):
    return dict(broker_add_per_lot_per_side=float(model['broker_add_per_lot_per_side']),
        products={p:{key:float(rule.get(key,0)) for key in KEYS} for p,rule in model['products'].items()})


def decision_cost_status(assessment):
    # A legacy boolean, including True, is insufficient for an economic verdict.
    if not isinstance(assessment,dict):return PENDING
    if assessment.get('status')==PENDING:return PENDING
    ticks=assessment.get('max_observed_slippage_ticks_per_side')
    if not isinstance(ticks,(int,float)) or isinstance(ticks,bool) or not math.isfinite(ticks) or ticks<0:return PENDING
    if ticks>2+1e-9:return 'INSUFFICIENT_COST_MARGIN'
    if assessment.get('fee_model_matches') is not True:return 'COST_MODEL_REPLAY_REQUIRED'
    if not assessment.get('ledger_coverage_complete'):return PENDING
    if not all(isinstance(assessment.get(k),str) and len(assessment[k])==64 for k in ('evidence_sha256','model_sha256')):return PENDING
    if assessment.get('status')!='VERIFIED_WITHIN_BUDGET':return PENDING
    return 'VERIFIED_WITHIN_BUDGET'


def load_cost_contract(inputs,lock,reg,data,model_path):
    """Verify immutable originals before the single-test token can be consumed."""
    inputs=Path(inputs);model_path=Path(model_path);model=read(model_path)
    if not lock.get('costs_certified'):return pending('no cost evidence claimed'),None
    path=inputs/'cost_evidence.json'
    if not path.is_file() or digest(path)!=lock.get('cost_evidence_sha256'):raise ValueError('cost evidence hash not bound')
    evidence=read(path)
    if evidence.get('schema_version')!=1:return pending('numeric cost schema required; booleans insufficient'),None
    if not evidence.get('owner') or not evidence.get('reviewer') or evidence['owner']==evidence['reviewer']:
        return pending('distinct source owner and reviewer required'),None
    if not evidence.get('fee_schedule_verified') or not evidence.get('quote_size_and_exit_costs_verified'):
        return pending('source review incomplete'),None
    if evidence.get('modeled_cost_sha256')!=digest(model_path):raise ValueError('evidence not bound to account cost model')
    sources=evidence.get('source_hashes',{})
    if not sources:return pending('original fee and quote files absent'),None
    for rel,sha in sources.items():
        source=(inputs/rel).resolve()
        if not source.is_relative_to(inputs.resolve()) or lock.get('source_hashes',{}).get(rel)!=sha or not source.is_file() or digest(source)!=sha:
            raise ValueError('cost original hash/path mismatch')
    rows=evidence.get('fee_periods',[]);quotes=evidence.get('quote_samples',[])
    if not rows or not quotes:return pending('dated fees and both execution sides required'),None
    norm=normalized_fees(model);match=True
    for row in rows:
        if row.get('source') not in sources:return pending('fee original missing'),None
        if row.get('symbol') not in data['legs'] or data['legs'][row['symbol']]['cs']!=row.get('csymbol'):
            return pending('fee actual contract mismatch'),None
        if row.get('start','9999')>row.get('end','0000'):return pending('invalid fee period'),None
        try:
            rates={k:float(row['rates'].get(k,0)) for k in KEYS};add=float(row['broker_add_per_lot_per_side'])
            if any(not math.isfinite(v) or v<0 for v in [*rates.values(),add]):raise ValueError()
            if rates!=norm['products'][row['csymbol'].split('.')[1]] or add!=norm['broker_add_per_lot_per_side']:match=False
        except (KeyError,TypeError,ValueError):return pending('invalid applicable fee numbers'),None
    for leg in data['legs'].values():
        selected={d for d in reg['score_trade_dates'] if leg['selection'].get(d)==leg['symbol']}
        if any(not any(r['symbol']==leg['symbol'] and r['start']<=day<=r['end'] for r in rows) for day in selected):
            return pending('selected contract fee period uncovered'),None
    validated=[];maximum=0.
    try:
        for q in quotes:
            if q['source'] not in sources or q['symbol'] not in data['legs']:raise ValueError('quote original/contract missing')
            leg=data['legs'][q['symbol']];spec=data['fees']['products'][leg['cs'].split('.')[1]]
            if q['csymbol']!=leg['cs'] or q['position_direction'] not in ('bull','bear') or q['offset'] not in ('open','close','today'):raise ValueError('quote identity')
            t=pd.Timestamp(q['decision_at']);created=pd.Timestamp(q['quote_at']);received=pd.Timestamp(q['received_at'])
            start=pd.Timestamp(q['session_start']);end=pd.Timestamp(q['session_end'])
            if any(x.tzinfo is None for x in (t,created,received,start,end)):raise ValueError('unknown timezone')
            if not start<=created<=received<=t<=end or (t-created).total_seconds()>1:raise ValueError('late or stale quote')
            # Match the exchange trading day, not the wall-clock date of night bars.
            if leg['daymap'].get(t.value+60_000_000_000,leg['daymap'].get(t.value))!=q['trading_date']:raise ValueError('quote trading day')
            buy=(q['position_direction']=='bull')==(q['offset']=='open')
            bid=float(q['bid']);ask=float(q['ask']);reference=float(q['reference_price']);price=float(q['executable_price'])
            tick=float(q['tick_size']);lots=int(q['lots']);depth=float(q['ask_size'] if buy else q['bid_size'])
            if not all(math.isfinite(x) and x>0 for x in (bid,ask,reference,price,tick,depth)) or bid>ask or lots<=0 or lots!=q['lots']:raise ValueError('invalid quote numbers')
            if abs(tick-spec['price_tick'])>1e-10 or depth<lots:raise ValueError('tick mismatch or insufficient one-level size')
            if abs(price-(ask if buy else bid))>tick*1e-7:raise ValueError('executable price must be the correct book side')
            adverse=max(0.,(price-reference)*(1 if buy else -1)/tick)
            maximum=max(maximum,adverse);validated.append(dict(q,reference_price=reference,lots=lots,adverse_slippage_ticks=adverse))
    except (KeyError,TypeError,ValueError,OverflowError) as exc:return pending('quote evidence incomplete: '+str(exc)),None
    assessment=dict(status='VERIFIED_WITHIN_BUDGET',fee_model_matches=match,ledger_coverage_complete=False,
        max_observed_slippage_ticks_per_side=maximum,evidence_sha256=digest(path),model_sha256=digest(model_path),
        measurement='MAX_ADVERSE_L1_EXECUTABLE_PROXY_TICKS; FEES_SEPARATE; NOT_OBSERVED_ORDER_FILLS',quote_samples=len(validated))
    # Pre-replay checks require numeric evidence but coverage is checked on final executions.
    if maximum>2+1e-9:assessment['status']='INSUFFICIENT_COST_MARGIN'
    elif not match:assessment['status']='COST_MODEL_REPLAY_REQUIRED'
    return assessment,dict(fee_periods=rows,quote_samples=validated,model=model,assessment=assessment)


def ledger_cost_coverage(result,contract,data,slip):
    """Every charged side must match dated rates, quote direction, size and clock."""
    if not contract:return dict(complete=False,reason='no numeric source contract')
    rows=contract['fee_periods'];quotes=contract['quote_samples'];missing=[];count=0
    def side(row,offset,t,day,reference,price):
        nonlocal count
        matches=[q for q in quotes if q['symbol']==row['symbol'] and q['position_direction']==row['direction']
            and q['offset']==offset and q['lots']==row['lots'] and q['trading_date']==day
            and pd.Timestamp(q['decision_at'])==pd.Timestamp(t) and abs(q['reference_price']-reference)<1e-7]
        fee_rows=[r for r in rows if r['symbol']==row['symbol'] and r['start']<=day<=r['end']]
        if not matches or not fee_rows:missing.append(dict(symbol=row['symbol'],offset=offset,time=t,reason='execution outside verified cost coverage'))
        rule=contract['model']['products'][row['csymbol'].split('.')[1]];mult=data['fees']['products'][row['csymbol'].split('.')[1]]['multiplier']
        count+=1
        return row['lots']*(price*mult*rule.get(offset+'_rate',0)+rule.get(offset+'_per_lot',0)+contract['model']['broker_add_per_lot_per_side'])
    for row in result['trades']:
        tick=data['fees']['products'][row['csymbol'].split('.')[1]]['price_tick'];sign=1 if row['direction']=='bull' else -1
        fee=side(row,'open',row['entry_bob'],row['entry_day'],row['entry_price']-sign*slip*tick,row['entry_price'])
        fee+=side(row,'today' if row['close_today'] else 'close',row['exit_time'],row['exit_day'],row['raw_exit_proxy'],row['exit_price'])
        if abs(fee-row['modeled_fee'])>.000001:missing.append(dict(symbol=row['symbol'],reason='charged fee differs from verified model'))
    for row in result['opened']:
        leg=data['legs'][row['symbol']];spec=data['fees']['products'][row['csymbol'].split('.')[1]];sign=1 if row['direction']=='bull' else -1
        day=leg['daymap'].get(pd.Timestamp(row['entry_bob']).value+60_000_000_000)
        fee=side(row,'open',row['entry_bob'],day,row['entry_price']-sign*slip*spec['price_tick'],row['entry_price'])
        if abs(fee-row['paid_entry_fee'])>.000001:missing.append(dict(symbol=row['symbol'],reason='open entry fee mismatch'))
    return dict(complete=count>0 and not missing,execution_sides=count,missing_count=len(missing),first_missing=missing[0] if missing else None)
