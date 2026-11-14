"""Memoize complete clock buckets without changing the frozen input window.

The cache is disposable; restart rebuilds it from the same committed M1 input.
No bucket is emitted with missing/duplicate minutes or a revised old row.
"""
from backtest.aggregation import combine_bars

POLICY = 'CORE_COMPLETE_BUCKET_CACHE_V1_20261114'


class CompleteBucketCache:
    def __init__(self):
        self.rows = {}
        self.buckets = {5: {}, 15: {}}
        self.complete = {5: {}, 15: {}}

    @staticmethod
    def key(bar, period):
        second = int(bar.eob.timestamp())
        width = period * 60
        return bar.symbol, ((second + width - 1) // width) * width

    def update(self, bars):
        incoming = {b.identity: b for b in bars}
        if len(incoming) != len(bars):
            raise ValueError('AGGREGATE_DUPLICATE_MINUTE')
        if any(b != incoming[k] for k, b in self.rows.items() if k in incoming):
            raise ValueError('AGGREGATE_REVISED_MINUTE')
        removed = self.rows.keys() - incoming.keys()
        added = incoming.keys() - self.rows.keys()
        for period in (5, 15):
            buckets, complete = self.buckets[period], self.complete[period]
            affected = set()
            for identity in removed:
                key = self.key(self.rows[identity], period)
                buckets[key].pop(identity)
                affected.add(key)
            for identity in added:
                b = incoming[identity]; key = self.key(b, period)
                buckets.setdefault(key, {})[identity] = b
                affected.add(key)
            for key in affected:
                complete.pop(key, None)
                group = buckets[key]
                if not group:
                    del buckets[key]
                    continue
                if len(group) != period:
                    continue
                items = sorted(group.values(), key=lambda b: b.eob)
                if all(b.eob.timestamp() == key[1]-(period-1-i)*60 for i, b in enumerate(items)):
                    complete[key] = combine_bars(items, str(period*60)+'s')
        self.rows = incoming
        return tuple([self.complete[p][k] for k in sorted(self.complete[p], key=lambda k: k[1])]
                     for p in (5, 15))
