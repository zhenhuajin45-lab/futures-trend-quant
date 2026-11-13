"""Official GM closed-Bar input. No Tick aggregation, orders or wallclock close."""
from datetime import datetime,timezone,timedelta
from dataclasses import replace
from pathlib import Path
import hashlib,json,math
from .paper_io import shared_open
from .paper_products import product_of

POLICY='GM_HOST_CLOSED_M1_V1_20261111'
SOURCE='GM_HOST_ON_BAR_CLOSED_M1'


def enabled(c):return c.get('gm_closed_m1_policy')==POLICY


def from_callback(raw,records,c,received):
    from futures_trend_quant.data.gm_bars import bar_from_mapping
    from .paper_tq import external_product,clock
    # The native BarLikeDict2 exposes fields through __getitem__, but its
    # apparent dict.get method is not callable on GM 3.0.186. Normalize at
    # this actual SDK boundary, as with the existing Tick adapter.
    fields={}
    for key in ('symbol','frequency','bob','eob','open','high','low','close','volume','amount','position'):
        try:fields[key]=raw[key]
        except (KeyError,TypeError,AttributeError):fields[key]=None
    raw=fields
    if any(raw.get(k) is None for k in ('volume','amount','position')):raise ValueError('GM_M1_UNKNOWN_SIZE')
    bar=bar_from_mapping(raw);cs=product_of(bar.symbol)
    if bar.symbol not in records or external_product(c,cs):raise ValueError('GM_M1_UNAUTHORIZED_SOURCE')
    if bar.frequency!='60s' or (bar.eob-bar.bob).total_seconds()!=60:raise ValueError('GM_M1_PERIOD')
    if any(t.tzinfo is None or t.second or t.microsecond for t in (bar.bob,bar.eob)):raise ValueError('GM_M1_CLOCK')
    zone=timezone(timedelta(hours=8))
    bar=replace(bar,bob=bar.bob.astimezone(zone),eob=bar.eob.astimezone(zone))
    if bar.eob>clock(received):raise ValueError('GM_M1_NOT_CLOSED_AT_RECEIPT')
    if any(not math.isfinite(v) or v<0 for v in (bar.volume,bar.position,bar.amount)):raise ValueError('GM_M1_UNKNOWN_SIZE')
    return cs,bar


class ClosedM1Book:
    def __init__(self,last_eob,calendar):
        self.last=last_eob;self.calendar=calendar;self.pending={};self.seen={}

    def add(self,bars,clocks):
        import pandas as pd
        old_rows=[]
        for bar in bars:
            if not self.calendar.section_for_bar(bar):raise ValueError('GM_M1_OUTSIDE_CALENDAR')
            prior=self.seen.get(bar.identity) or (self.pending.get(bar.eob) or (None,None))[0]
            if prior is not None:
                if prior!=bar:raise ValueError('GM_CLOSED_M1_REVISION')
                continue
            if bar.eob<=self.last:
                # The core compares older overlap against its frozen input. It
                # cannot establish another vote, and must not be silently trusted.
                old_rows.append((bar,dict(clocks)));continue
            self.pending[bar.eob]=(bar,dict(clocks))
        ready=[]
        while self.pending:
            index=int(self.calendar.ns.searchsorted(pd.Timestamp(self.last).value,side='right'))
            if index>=len(self.calendar.clock):break
            next_eob=self.calendar.clock[index].to_pydatetime()
            item=self.pending.pop(next_eob,None)
            if item is None:break
            bar,proof=item;self.last=bar.eob;self.seen[bar.identity]=bar;ready.append(item)
        if len(self.seen)>24000:
            for k in list(self.seen)[:-24000]:self.seen.pop(k)
        return old_rows+ready


def restore(config,bars,last_eob,proof=None,*,asof=None):
    """A checkpoint can never skip closed inputs consumed from a callback."""
    from futures_trend_quant.data.gm_bars import bars_from_records
    path=Path(config['state_dir'])/'gm_core_m1.jsonl';known={b.identity:b for b in bars}
    if path.exists():
        with shared_open(path) as stream:raw=stream.read()
    else:raw=b''
    if proof is not None:
        size=int(proof['bytes'])
        if Path(proof['path']).resolve()!=path.resolve() or size<=0 or size>len(raw) or not raw[:size].endswith(b'\n') or hashlib.sha256(raw[:size]).hexdigest()!=proof['sha256']:
            raise ValueError('GM_INPUT_JOURNAL_PREFIX_CHANGED')
    if asof is not None:
        if asof.tzinfo is None:raise ValueError('GM_NOTICE_ASOF_TIMEZONE')
        if raw and proof is None:raise ValueError('GM_NOTICE_COMMITTED_PREFIX_REQUIRED')
        raw=raw[:int(proof['bytes'])] if proof else raw
    available={b.identity:b for b in bars if asof is None or b.eob<=asof}
    for line in raw.split(b'\n')[:-1]:
        row=json.loads(line)
        if row.get('policy')!=POLICY or row.get('csymbol')!=config['csymbol']:raise ValueError('GM_INPUT_JOURNAL_IDENTITY')
        bar=bars_from_records([row['bar']])[0]
        if not bars or bar.symbol!=bars[0].symbol or bar.frequency!='60s':raise ValueError('GM_INPUT_JOURNAL_INSTRUMENT')
        if bar.identity in known and known[bar.identity]!=bar:raise ValueError('GM_INPUT_JOURNAL_REVISION')
        known[bar.identity]=bar
        if asof is not None:
            received=datetime.fromisoformat(row['source_known_at']);recorded=datetime.fromisoformat(row['recorded_at'])
            if received.tzinfo is None or recorded.tzinfo is None or received<bar.eob or recorded<received:raise ValueError('GM_NOTICE_JOURNAL_CLOCK')
            if max(received,recorded,bar.eob)<=asof:available[bar.identity]=bar
    restored=sorted(known.values(),key=lambda b:b.eob)[-24000:]
    if proof and last_eob and (not restored or restored[-1].eob<datetime.fromisoformat(str(last_eob))):
        raise ValueError('GM_CHECKPOINT_USED_INPUT_PREFIX_MISSING')
    return sorted(available.values(),key=lambda b:b.eob)[-24000:] if asof is not None else restored
