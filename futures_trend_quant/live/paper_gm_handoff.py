"""GM queue isolation. Quotes are replaceable state; bars and orders are not."""
from collections import OrderedDict
import queue,threading
from .paper_quote_feed import supersedes

POLICY='GM_PRIORITY_HANDOFF_V1_20261112'


def enabled(c):return c.get('gm_handoff_policy')==POLICY


class LatestQuotes:
    """In-process callback mailbox; retain original event and receipt clocks."""
    def __init__(self):self.items=OrderedDict();self.lock=threading.Lock();self.replaced=0;self.older=0
    def put_nowait(self,item):
        symbol,quote,received=item
        with self.lock:
            old=self.items.get(symbol)
            if old and not supersedes(old[1],quote):self.older+=1;return
            if old:self.replaced+=1
            self.items[symbol]=item
    def get_nowait(self):
        with self.lock:
            if not self.items:raise queue.Empty
            return self.items.popitem(last=False)[1]
    def empty(self):return self.qsize()==0
    def qsize(self):
        with self.lock:return len(self.items)


def ordered_products(r):
    from .paper_tq import external_product
    groups=[[cs for cs in r.products if bool(external_product(r.c,cs))==external] for external in (False,True)]
    result=[]
    for number,items in enumerate(groups):
        if not items:continue
        key='_priority_cursor_'+str(number);start=getattr(r,key,0)%len(items);setattr(r,key,start+1)
        result+=items[start:]+items[:start]
    return result
