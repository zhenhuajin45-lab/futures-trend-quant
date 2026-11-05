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
# Do not import the strategy/core stack or gm market SDK into this old native SDK process.


def serve(config_path):
    from hashlib import sha256
    config_digest=sha256(Path(config_path).read_bytes()).hexdigest()
    c=json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
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
    def snapshot():
        cash=api.get_cash(account=account)
        if cash is None:raise ValueError('missing cash')
        return dict(cash=obj(cash),positions=[obj(x) for x in api.get_positions(account=account)],
            orders=[obj(x) for x in api.get_orders(account=account)],
            active=[obj(x) for x in api.get_unfinished_orders(account=account)],
            fills=[obj(x) for x in api.get_execution_reports(account=account)])
    seen={}
    for line in sys.stdin:
        request=json.loads(line);rid=request['id'];op=request['op'];args=request.get('args',{})
        if rid in seen:
            print(json.dumps(seen[rid]),flush=True);continue
        submit_invoked=False
        try:
            if sha256(Path(config_path).read_bytes()).hexdigest()!=config_digest or sha256(binding.read_bytes()).hexdigest()!=c['account_binding_sha256']:
                raise ValueError('paper account/config changed; orders and default fallback denied')
            if request.get('account_id')!=account:raise ValueError('missing/wrong explicit account')
            if set(ctx.logined_accounts)!={account}:raise ValueError('account context changed')
            with contextlib.redirect_stdout(sys.stderr):
                if op=='snapshot':data=snapshot()
                elif op=='submit':
                    if c.get('paper_orders_enabled') is not True:raise ValueError('paper orders disabled')
                    symbol=args['symbol'];side=int(args['side']);effect=int(args['position_effect']);price=float(args['price']);qty=int(args['volume'])
                    if not symbol.startswith('DCE.m') or not symbol[5:].isdigit() or side not in (1,2) or effect not in (1,2,3,4):raise ValueError('invalid paper instrument/effect')
                    if qty!=1 or not math.isfinite(price) or price<=0:raise ValueError('one bounded limit lot only')
                    qtime=datetime.fromisoformat(args['quote_event_at']);rtime=datetime.fromisoformat(args['quote_received_at'])
                    now=datetime.now(timezone.utc)
                    if qtime.tzinfo is None or rtime.tzinfo is None or not -1<=(now-qtime).total_seconds()<=3 or not 0<=(now-rtime).total_seconds()<=3:raise ValueError('stale quote at final broker exit')
                    bid=float(args['quote_bid']);ask=float(args['quote_ask']);tick=float(args['tick'])
                    if not all(math.isfinite(x) and x>0 for x in (bid,ask,tick)) or not bid<=ask or ask-bid>4*tick+1e-8:raise ValueError('invalid bounded quote')
                    if side==1 and price>ask+1e-8 or side==2 and price<bid-1e-8:raise ValueError('limit beyond observable opponent')
                    if effect==1:
                        cash=obj(api.get_cash(account=account));poses=[obj(x) for x in api.get_positions(account=account)]
                        active_orders=api.get_unfinished_orders(account=account)
                        nav=float(cash['nav']);available=float(cash['available'])
                        if any(int(p['volume'])>0 for p in poses) or active_orders:raise ValueError('simulation account already has exposure or active orders')
                        stop=float(args['stop']);mult=float(args['multiplier']);margin=float(args['margin_ratio'])*mult*price
                        if not all(math.isfinite(x) and x>0 for x in (stop,mult,float(args['margin_ratio']))) or float(args['margin_ratio'])>1:raise ValueError('invalid contract/risk metadata')
                        if not (stop<price if side==1 else stop>price):raise ValueError('stop invalid at final broker exit')
                        if abs(price-stop)*mult>nav*.0025 or margin>available or max(0.,nav-available)+margin>nav*.2:raise ValueError('paper risk/margin limit at final broker exit')
                        if nav<=float(args['day_nav'])*.99 or nav<=float(args['peak_nav'])*.95:raise ValueError('paper loss/drawdown limit at final broker exit')
                    if effect!=1:
                        positions=api.get_positions(account=account);position_side=1 if side==2 else 2
                        allowed=sum(int(x.available) for x in positions if x.account_id==account and x.symbol==symbol and x.side==position_side)
                        if allowed<qty:raise ValueError('close exceeds confirmed available position')
                    if (datetime.now(timezone.utc)-qtime).total_seconds()>3:raise ValueError('quote became stale before native submit')
                    submit_invoked=True
                    data=[obj(x) for x in api.order_volume(symbol=symbol,volume=qty,side=side,order_type=1,position_effect=effect,price=price,account=account)]
                    if not data:raise RuntimeError('SUBMIT_OUTCOME_UNKNOWN_NO_RETRY')
                elif op=='cancel':
                    oid=args['cl_ord_id']
                    match=next((x for x in api.get_unfinished_orders(account=account) if x.account_id==account and x.cl_ord_id==oid),None)
                    if match is None:data=dict(status='NOT_CURRENTLY_CANCELABLE_RECONCILE')
                    else:
                        api.order_cancel([dict(cl_ord_id=oid,account_id=account)])
                        data=dict(status='CANCEL_REQUESTED_NOT_CONFIRMED')
                else:raise ValueError('unsupported bridge action')
            reply=dict(id=rid,ok=True,data=data)
        except Exception as exc:reply=dict(id=rid,ok=False,error_type=type(exc).__name__,submit_may_have_reached_broker=submit_invoked,reason=str(exc)[:160] if not isinstance(exc,OSError) else 'SIM_CONNECTION_ERROR')
        seen[rid]=reply
        print(json.dumps(reply,ensure_ascii=False),flush=True)


if __name__=='__main__':serve(sys.argv[1])
