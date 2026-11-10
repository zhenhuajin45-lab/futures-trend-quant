"""Pandas-free final native quote validation. Never reprices an intent."""
from datetime import datetime
import json
import math
from pathlib import Path
from .paper_io import read_json


def latest_quote(root, config, config_digest, args, now):
    if int(args.get('position_effect',1))==1 and config.get('clock_policy_version'):
        from .paper_clock_health import require_clock
        require_clock(dict(config,config_sha256=config_digest),root=root)
        from .paper_service_health import require_service
        require_service(dict(config,root=str(root),config_sha256=config_digest),now,request=args)
    symbol=args['symbol']
    if not all(c.isalnum() or c in '._' for c in symbol):
        raise ValueError('invalid quote cache instrument')
    path=Path(root)/config['evidence_dir']/'latest_quotes'/(symbol+'.json')
    try:row=read_json(path)
    except (OSError,ValueError) as exc:raise ValueError('latest quote cache unreadable; no native submit') from exc
    if row.get('config_sha256')!=config_digest or row.get('symbol')!=symbol:
        raise ValueError('latest quote identity/profile mismatch')
    q=row['quote']
    sources={'GM_BULK_CURRENT_POLL'}
    if config.get('quote_policy_version')=='GM_HOST_TICK_PRIMARY_V1_20261109':sources.add('GM_HOST_TICK_CALLBACK')
    if q.get('source') not in sources:raise ValueError('untrusted final quote source')
    event=datetime.fromisoformat(q['created_at']);received=datetime.fromisoformat(row['received_at'])
    if event.tzinfo is None or received.tzinfo is None or not -1<=(now-event).total_seconds()<=3 or not 0<=(now-received).total_seconds()<=3:
        raise ValueError('latest quote stale/future at native submit')
    bid=float(q['bid']);ask=float(q['ask']);tick=float(q['tick'])
    if not all(math.isfinite(x) and x>0 for x in (bid,ask,tick)) or bid>ask or ask-bid>4*tick+1e-8:
        raise ValueError('latest quote invalid book/spread')
    if not all(math.isfinite(float(q[k])) and float(q[k])>=1 for k in ('bid_size','ask_size')):
        raise ValueError('latest quote opponent size unavailable')
    if not math.isclose(tick,float(args['tick']),rel_tol=1e-10):raise ValueError('latest quote tick mismatch')
    price=float(args['price']);side=int(args['side'])
    if int(args.get('position_effect',1))==1 and args.get('anchor') is not None:
        last=float(q.get('last',0));anchor=float(args['anchor'])
        if not math.isfinite(last) or last<=0 or (last<anchor if side==1 else last>anchor):
            raise ValueError('anchor broken/unknown in latest native quote')
    if side==1 and price>ask+1e-8 or side==2 and price<bid-1e-8:
        raise ValueError('fixed limit beyond latest observable opponent')
    return dict(symbol=symbol,event_at=q['created_at'],received_at=row['received_at'],
                validated_at=now.isoformat(),bid=bid,ask=ask,bid_size=q['bid_size'],ask_size=q['ask_size'],
                source=q['source'],tick=tick,limit_price_unchanged=price)
