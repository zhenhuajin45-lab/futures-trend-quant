"""GM tick quote adapter. No bars, permissions, signals or orders are created."""
from datetime import datetime

POLICY = 'GM_HOST_TICK_PRIMARY_V1_20261109'
TICK_SOURCE = 'GM_HOST_TICK_CALLBACK'


class TickFieldError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


def field(value,name,default=None,required=False):
    # GM's native TickLikeDict2 implements __getitem__, but its .get attribute
    # is None. Do not infer dict methods from its dict-like public name.
    try:return value[name]
    except (KeyError,TypeError,AttributeError,IndexError):
        if required:raise TickFieldError('TICK_FIELD_MISSING_'+name.upper()) from None
        return default


def clock(value):
    result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('QUOTE_TIMEZONE_UNKNOWN')
    return result


def from_tick(tick, records):
    symbol=field(tick,'symbol',required=True)
    if symbol not in records:raise ValueError('TICK_CONTRACT_NOT_AUTHORIZED')
    created=field(tick,'created_at',required=True)
    clock(created)
    book=field(tick,'quotes',required=True)
    if not isinstance(book,(list,tuple)) or not book:raise TickFieldError('TICK_BOOK_MISSING')
    level=book[0]
    return symbol,dict(created_at=str(created),last=field(tick,'price',required=True),
        tick=float(records[symbol]['tick']),bid=field(level,'bid_p',0),ask=field(level,'ask_p',0),
        bid_size=field(level,'bid_v',0),ask_size=field(level,'ask_v',0),source=TICK_SOURCE)


def supersedes(previous, incoming):
    """A delayed poll must not replace a newer subscribed tick in either cache."""
    if previous is None:return True
    old,new=clock(previous['created_at']),clock(incoming['created_at'])
    if new<old:return False
    if new==old and previous.get('source')==TICK_SOURCE and incoming.get('source')!=TICK_SOURCE:return False
    return True
