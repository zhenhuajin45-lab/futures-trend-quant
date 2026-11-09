"""GM tick quote adapter. No bars, permissions, signals or orders are created."""
from datetime import datetime

POLICY = 'GM_HOST_TICK_PRIMARY_V1_20261109'
TICK_SOURCE = 'GM_HOST_TICK_CALLBACK'


def clock(value):
    result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('QUOTE_TIMEZONE_UNKNOWN')
    return result


def from_tick(tick, records):
    symbol=tick['symbol']
    if symbol not in records:raise ValueError('TICK_CONTRACT_NOT_AUTHORIZED')
    clock(tick['created_at'])
    book=tick.get('quotes') or [];level=book[0] if book else {}
    return symbol,dict(created_at=str(tick['created_at']),last=tick.get('price'),
        tick=float(records[symbol]['tick']),bid=level.get('bid_p',0),ask=level.get('ask_p',0),
        bid_size=level.get('bid_v',0),ask_size=level.get('ask_v',0),source=TICK_SOURCE)


def supersedes(previous, incoming):
    """A delayed poll must not replace a newer subscribed tick in either cache."""
    if previous is None:return True
    old,new=clock(previous['created_at']),clock(incoming['created_at'])
    if new<old:return False
    if new==old and previous.get('source')==TICK_SOURCE and incoming.get('source')!=TICK_SOURCE:return False
    return True
