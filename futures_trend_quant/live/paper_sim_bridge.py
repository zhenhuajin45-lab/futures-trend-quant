"""Isolated Python 3.10 GM official SIM RPC worker. Never a live endpoint.

All native functions get the exact account. Vendor silent empty-on-error behavior
is converted to a hard failure before reconciliation can interpret an empty list.
"""
from pathlib import Path
import contextlib
import json
import math
import os
import sys
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from futures_trend_quant.live.paper_products import product_of,bucket_available,registered_pool
from futures_trend_quant.live.paper_anchor_guard import validate_anchor_proof,validate_execution_proof
from futures_trend_quant.live.paper_quote_cache import latest_quote
# Do not import the strategy/core stack or gm market SDK into this old native SDK process.


def _serve(config_path,role='WRITER'):
    if role not in ('WRITER','READ_ONLY'):raise ValueError('unknown bridge role')
    from hashlib import sha256
    config_digest=sha256(Path(config_path).read_bytes()).hexdigest()
    c=json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    universe=c.get('universe',['DCE.M'])
    registered_pool(universe,c.get('pool_policy_version'),allow_single=True)
    if c.get('endpoint')!='api.myquant.cn:9000' or c.get('live_orders_enabled') is not False or c.get('transport')!='OFFICIAL_SIM_GMTRADE':raise ValueError('simulation endpoint required')
    binding=ROOT/c['account_binding']
    if sha256(binding.read_bytes()).hexdigest()!=c['account_binding_sha256']:raise ValueError('account binding changed')
    proof=json.loads(binding.read_text(encoding='utf-8-sig'));account=proof['account_id']
    if proof.get('endpoint')!=c['endpoint'] or proof.get('source')!='GM_OFFICIAL_SIM_ACCOUNT_CASH_ID_MATCH' or proof.get('user_designated_futures_sim') is not True:raise ValueError('simulation proof required')
    with contextlib.redirect_stdout(sys.stderr):
        from gmtrade import api
        from gmtrade.api import trade
        from gmtrade.api.storage import ctx
        from google.protobuf.json_format import MessageToDict
        original=trade.c_status_fail
        def strict(status,name):
            failed=original(status,name)
            if failed:raise RuntimeError('SIM_RPC_STATUS_'+str(status))
            return failed
        trade.c_status_fail=strict
        api.set_token(os.environ['GM_TOKEN']);api.set_endpoint('api.myquant.cn:9000')
        api.login(api.account(account_id=account))
        if set(ctx.logined_accounts)!={account}:raise ValueError('not exactly one simulation account')
    def obj(x):
        r=MessageToDict(x,preserving_proto_field_name=True,including_default_value_fields=True,use_integers_for_enums=True)
        if r.get('account_id')!=account:raise ValueError('foreign account row')
        return r
    from futures_trend_quant.live.paper_native_trace import NativeTrace
    trace=NativeTrace(ROOT,c,role)
    def native(stage,fn):return trace.call(rid,op,stage,fn)
    def snapshot():
        cash=native('cash',lambda:api.get_cash(account=account))
        if cash is None:raise ValueError('missing cash')
        return dict(cash=obj(cash),positions=[obj(x) for x in native('positions',lambda:api.get_positions(account=account))],
            orders=[obj(x) for x in native('orders',lambda:api.get_orders(account=account))],
            active=[obj(x) for x in native('active_orders',lambda:api.get_unfinished_orders(account=account))],
            fills=[obj(x) for x in native('fills',lambda:api.get_execution_reports(account=account))])
    seen={}
    for line in sys.stdin:
        request=json.loads(line);rid=request['id'];op=request['op'];args=dict(request.get('args',{}),request_id=rid)
        if rid in seen:
            print(json.dumps(seen[rid]),flush=True);continue
        submit_invoked=False;cancel_invoked=False
        execution_quote=None
        try:
            if not isinstance(rid,str) or not rid:raise ValueError('RPC_REQUEST_ID_REQUIRED')
            if role=='READ_ONLY' and op!='snapshot':raise ValueError('READ_ONLY_CAPABILITY_DENIED')
            if sha256(Path(config_path).read_bytes()).hexdigest()!=config_digest or sha256(binding.read_bytes()).hexdigest()!=c['account_binding_sha256']:
                raise ValueError('paper account/config changed; orders and default fallback denied')
            if request.get('account_id')!=account:raise ValueError('missing/wrong explicit account')
            if set(ctx.logined_accounts)!={account}:raise ValueError('account context changed')
            with contextlib.redirect_stdout(sys.stderr):
                if op=='snapshot':data=snapshot()
                elif op=='exit_snapshot':
                    data=dict(positions=[obj(x) for x in native('exit_positions',lambda:api.get_positions(account=account))],
                              active=[obj(x) for x in native('exit_active_orders',lambda:api.get_unfinished_orders(account=account))])
                elif op=='submit':
                    if c.get('paper_orders_enabled') is not True:raise ValueError('paper orders disabled')
                    symbol=args['symbol'];side=int(args['side']);effect=int(args['position_effect']);price=float(args['price']);qty=int(args['volume'])
                    cs=product_of(symbol)
                    if cs not in universe or args.get('csymbol',cs)!=cs or side not in (1,2) or effect not in (1,2,3,4):raise ValueError('invalid paper instrument/effect')
                    if qty!=1 or not math.isfinite(price) or price<=0:raise ValueError('one bounded limit lot only')
                    record=None
                    if c.get('schema_version')==2:
                        registry=json.loads((ROOT/c['market_registry']).read_text(encoding='utf8'))
                        if registry.get('config_sha256')!=config_digest:raise ValueError('market registry/config mismatch')
                        record=registry['contracts'].get(symbol)
                        if not record or record['csymbol']!=cs:raise ValueError('actual contract not registered')
                        for key,argument in [('multiplier','multiplier'),('margin_ratio','margin_ratio'),('tick','tick')]:
                            if not math.isclose(float(record[key]),float(args[argument]),rel_tol=1e-10):raise ValueError('contract metadata mismatch at final broker exit')
                    qtime=datetime.fromisoformat(args['quote_event_at']);rtime=datetime.fromisoformat(args['quote_received_at'])
                    now=datetime.now(timezone.utc)
                    if qtime.tzinfo is None or rtime.tzinfo is None or not -1<=(now-qtime).total_seconds()<=3 or not 0<=(now-rtime).total_seconds()<=3:raise ValueError('stale quote at final broker exit')
                    bid=float(args['quote_bid']);ask=float(args['quote_ask']);tick=float(args['tick'])
                    if not all(math.isfinite(x) and x>0 for x in (bid,ask,tick)) or not bid<=ask or ask-bid>4*tick+1e-8:raise ValueError('invalid bounded quote')
                    if abs(price/tick-round(price/tick))>1e-6:raise ValueError('price outside tick grid')
                    if side==1 and price>ask+1e-8 or side==2 and price<bid-1e-8:raise ValueError('limit beyond observable opponent')
                    if effect==1:
                        if (c.get('entry_policy_version')=='PAPER_ENTRY_V5')!=(args.get('permission_policy')=='PAPER_ENTRY_V5'):raise ValueError('ENTRY_POLICY_VERSION_MISMATCH')
                        if args.get('permission_policy')=='PAPER_ENTRY_V5' and args.get('path')=='B':
                            base=json.loads((ROOT/c['base_strategy']).read_text(encoding='utf-8-sig')) if c.get('base_strategy') else {'rules':{'path_b_h1_exception':False}}
                            if (args.get('entry_policy_proof') or {}).get('b_h1_exception')!=bool(base['rules']['path_b_h1_exception']):raise ValueError('V5_H1_EXCEPTION_MISMATCH')
                        validate_anchor_proof(args,symbol,side,price)
                        if c.get('strategy_name')=='S0_SHORT_PAPER_CORE_V2':validate_execution_proof(args,now,side)
                        cash=obj(native('final_cash',lambda:api.get_cash(account=account)));poses=[obj(x) for x in native('final_positions',lambda:api.get_positions(account=account))]
                        active_orders=[obj(x) for x in native('final_active_orders',lambda:api.get_unfinished_orders(account=account))]
                        nav=float(cash['nav']);available=float(cash['available'])
                        if any(int(p['volume'])>0 and product_of(p['symbol'])==cs for p in poses) or any(product_of(o['symbol'])==cs for o in active_orders):raise ValueError('same product already has exposure or active orders')
                        if sum(int(p['volume'])>0 for p in poses)+sum(int(o.get('position_effect',1))==1 for o in active_orders)>=int(c.get('max_positions',1)):raise ValueError('portfolio position limit at final broker exit')
                        stop=float(args['stop']);mult=float(args['multiplier']);margin=float(args['margin_ratio'])*mult*price
                        if not all(math.isfinite(x) and x>0 for x in (stop,mult,float(args['margin_ratio']))) or float(args['margin_ratio'])>1:raise ValueError('invalid contract/risk metadata')
                        if not (stop<price if side==1 else stop>price):raise ValueError('stop invalid at final broker exit')
                        if abs(price-stop)*mult>nav*min(.0025,float(c.get('risk_pct',.0025))) or margin>available or max(0.,nav-available)+margin>nav*min(.2,float(c.get('margin_usage_pct',.2))):raise ValueError('paper risk/margin limit at final broker exit')
                        if nav<=float(args['day_nav'])*.99 or nav<=float(args['peak_nav'])*.95:raise ValueError('paper loss/drawdown limit at final broker exit')
                    if effect!=1:
                        if symbol.startswith('SHFE.') and effect not in (3,4):raise ValueError('SHFE requires explicit today/yesterday close')
                        positions=native('close_available',lambda:api.get_positions(account=account));position_side=1 if side==2 else 2
                        allowed=sum(bucket_available(x,effect) for x in positions if x.account_id==account and x.symbol==symbol and x.side==position_side)
                        if allowed<qty:raise ValueError('close exceeds confirmed available position')
                    if c.get('execution_policy_version') not in ('POSTCLOSE_EXEC_V3_20261106','PAPER_EXEC_V5') and (datetime.now(timezone.utc)-qtime).total_seconds()>3:
                        raise ValueError('quote became stale before native submit')
                    if effect==1 and c.get('strategy_name')=='S0_SHORT_PAPER_CORE_V2':
                        validate_execution_proof(args,datetime.now(timezone.utc),side)
                    if effect==1 and c.get('state_dir'):
                        control=ROOT/c['state_dir']/'control.json'
                        if control.exists() and json.loads(control.read_text(encoding='utf8')).get('stop_new_open'):raise ValueError('operator stop-new at final broker exit')
                        if c.get('execution_policy_version') in ('POSTCLOSE_EXEC_V3_20261106','PAPER_EXEC_V5'):
                            state=json.loads((ROOT/c['state_dir']/'execution.json').read_text(encoding='utf8'))
                            if state.get('config_sha256')!=config_digest or state.get('account_id')!=account:raise ValueError('risk state identity mismatch')
                            if state.get('pause_new') or state.get('halt_reason'):raise ValueError('risk stop-new at final broker exit')
                            risk=state.get('loss_cooldown',{})
                            if risk.get('until') and datetime.now(timezone.utc)<datetime.fromisoformat(risk['until']):raise ValueError('loss cooldown at final broker exit')
                            if risk.get('blocked_through_day') and state.get('day','')<=risk['blocked_through_day']:raise ValueError('loss trading-day cooldown at final broker exit')
                    if record is not None:
                        t=datetime.now(timezone.utc).timestamp()
                        windows=record['entry_windows'] if effect==1 else record['trade_windows']
                        if not any(a<=t<b for a,b in windows):raise ValueError('outside registered execution window')
                    if c.get('execution_policy_version') in ('POSTCLOSE_EXEC_V3_20261106','PAPER_EXEC_V5'):
                        execution_quote=latest_quote(ROOT,c,config_digest,args,datetime.now(timezone.utc))
                        if effect==1 and c.get('strategy_name')=='S0_SHORT_PAPER_CORE_V2':validate_execution_proof(args,datetime.now(timezone.utc),side)
                    submit_invoked=True
                    data=[obj(x) for x in native('order_volume',lambda:api.order_volume(symbol=symbol,volume=qty,side=side,order_type=1,position_effect=effect,price=price,account=account))]
                    if not data:raise RuntimeError('SUBMIT_OUTCOME_UNKNOWN_NO_RETRY')
                elif op=='cancel':
                    oid=args['cl_ord_id']
                    match=next((x for x in native('cancel_active_orders',lambda:api.get_unfinished_orders(account=account)) if x.account_id==account and x.cl_ord_id==oid),None)
                    if match is None:data=dict(status='NOT_CURRENTLY_CANCELABLE_RECONCILE')
                    else:
                        cancel_invoked=True
                        native('order_cancel',lambda:api.order_cancel([dict(cl_ord_id=oid,account_id=account)]))
                        data=dict(status='CANCEL_REQUESTED_NOT_CONFIRMED')
                else:raise ValueError('unsupported bridge action')
            reply=dict(id=rid,ok=True,data=data,native_submit_invoked=submit_invoked,native_cancel_invoked=cancel_invoked,final_execution_quote=execution_quote)
        except Exception as exc:reply=dict(id=rid,ok=False,error_type=type(exc).__name__,submit_may_have_reached_broker=submit_invoked,native_submit_invoked=submit_invoked,native_cancel_invoked=cancel_invoked,final_execution_quote=execution_quote,reason=str(exc)[:160] if not isinstance(exc,OSError) else 'SIM_CONNECTION_ERROR',service_diagnostic=getattr(exc,'details',None))
        seen[rid]=reply
        print(json.dumps(reply,ensure_ascii=False),flush=True)


def serve(config_path,role='WRITER'):
    c=json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    if role=='WRITER' and c.get('execution_policy_version')=='PAPER_EXEC_V5':
        from futures_trend_quant.live.paper_writer_lock import writer_lease
        account=json.loads((ROOT/c['account_binding']).read_text(encoding='utf-8-sig'))['account_id']
        with writer_lease(account):return _serve(config_path,role)
    return _serve(config_path,role)


if __name__=='__main__':serve(sys.argv[1],sys.argv[2] if len(sys.argv)>2 else 'WRITER')
