"""One causal snapshot per closed M15 / candidate. Never returns an order decision."""
from datetime import datetime, timezone
from .models import POLICY, SCHEMA, Zone, clock, digest, parameters
from .structure import confirmed_pivots, structure
from .zones import pivot_zone, previous_day_zones, mature_range, short_box, position, touch
from .patterns import quality
from .breakout import advance
from .room import available_room
from .invalidation import hypothesis,observe


class Pipeline:
    def __init__(self, state, config=None):
        self.params=parameters(config)
        self.parameter_hash=digest(self.params)
        self.state=state

    def snapshot(self, *, symbol, csymbol, m15, m5, received, tick, asof, trading_day,
                 session_id, source_prefix_sha256, candidate=None, complete_days=(), day_by_id=None,
                 completeness='COMPLETE_CLOSED_PREFIX'):
        asof=clock(asof)
        if not m15 or any(b.symbol!=symbol for b in m15+m5):raise ValueError('PA_CONTRACT_IDENTITY')
        if any(b.eob>asof for b in m15+m5):raise ValueError('PA_FUTURE_BAR')
        if any(received.get(b.identity) is not None and clock(received[b.identity])>asof for b in m15+m5):
            raise ValueError('PA_FUTURE_RECEIVE_CLOCK')
        receipt_complete=all(received.get(b.identity) for b in m15+m5)
        product=self.state.data['products'].setdefault(symbol,dict(zones={},pivots={},breakouts={},touches={},mother_id=None))
        last=m15[-1]
        key=digest([POLICY,self.parameter_hash,symbol,last.identity,None if not m5 else m5[-1].identity,
                    None if not candidate else candidate.get('candidate_evidence_id')])
        if key in self.state.data['emitted']:return None
        from futures_trend_quant.core.facts.indicators import atr
        atr_values=atr(m15);atr_value=atr_values[-1]
        pivots=confirmed_pivots(m15,received,asof,self.params['pivot_k'],self.params['leg_min_atr'])
        new_pivots=[]
        for p in pivots:
            if p.event_id not in product['pivots']:
                row=p.row()
                # Rule confirmation, actual receive, and computation must all exist.
                row['known_at']=None if p.known_at is None else max(asof,clock(p.known_at)).isoformat()
                product['pivots'][p.event_id]=row
                new_pivots.append(row)
                z=pivot_zone(p,tick,self.params).row();z['known_at']=row['known_at']
                product['zones'].setdefault(z['zone_id'],z)
        from .models import PivotFact
        current_ids={p.event_id for p in pivots}
        prior_members=product.get('pivot_membership',[])
        membership_changes=dict(added=sorted(current_ids-set(prior_members)),superseded_or_out_of_window=sorted(set(prior_members)-current_ids))
        product['pivot_membership']=sorted(current_ids)
        current=[PivotFact(**row) for pid,row in product['pivots'].items() if pid in current_ids]
        current.sort(key=lambda p:(p.confirmed_at,p.bar_id))
        struct=structure(current,last.close,tick) if completeness=='COMPLETE_CLOSED_PREFIX' and receipt_complete else dict(structure_state='UNKNOWN',reason_codes=['INPUT_GAP_OR_CLOCK_UNKNOWN'],protected_pivot=None)
        if atr_value:
            for z in previous_day_zones(m15,day_by_id or {},complete_days,trading_day,tick,atr_value,asof.isoformat(),self.params):
                product['zones'].setdefault(z.zone_id,z.row())
        zone_status=product.setdefault('zone_status',{})
        for zid,zrow in product['zones'].items():
            if zrow['kind']=='M15_PIVOT' and not set(zrow['source_ids'])&current_ids:
                zone_status.setdefault(zid,dict(status='RETIRED_SOURCE_SUPERSEDED_OR_WINDOW',known_at=asof.isoformat()))
        zones=[Zone(**row) for zid,row in product['zones'].items() if row.get('known_at') and clock(row['known_at'])<=asof and zid not in zone_status]
        episodes=[]
        trigger=m5[-1] if m5 and m5[-1].eob>=last.eob else last
        for z in zones:
            product['touches'][z.zone_id]=touch(z,trigger,product['touches'].get(z.zone_id,{}))
            prior=product['breakouts'].get(z.zone_id)
            episode=advance(z,trigger,tick,prior,asof.isoformat())
            if episode.get('status')!='UNKNOWN':
                product['breakouts'][z.zone_id]=episode
                if episode!=prior:episodes.append(episode)
                if episode.get('role_change'):
                    new=Zone(digest([z.zone_id,episode['episode_id'],'ROLE_CHANGE']),z.kind,episode['role_change'],
                             z.lower,z.upper,(z.zone_id,episode['episode_id']),trigger.eob.isoformat(),asof.isoformat())
                    product['zones'].setdefault(new.zone_id,new.row())
                    zone_status[z.zone_id]=dict(status='ROLE_CHANGED',replacement=new.zone_id,known_at=asof.isoformat())
        box=mature_range(m15[:-1],current,asof,self.params)
        m5prior=[b for b in m5 if b.eob<trigger.eob]
        short=short_box(m5prior,atr_value,self.params)
        if short.get('status')=='FORMED':
            width=max(tick*self.params['zone_ticks'],atr_value*self.params['zone_atr'])
            for role,price in [('SUPPORT',short['lower']),('RESISTANCE',short['upper'])]:
                z=Zone(digest(['M5_SHORT_BOX',role,short['source_ids']]),'M5_SHORT_BOX',role,
                       price-width,price+width,tuple(short['source_ids']),m5prior[-1].eob.isoformat(),asof.isoformat())
                product['zones'].setdefault(z.zone_id,z.row())
        previous=m15[-2] if len(m15)>1 else None
        mother=next((b for b in m15 if b.identity==product.get('mother_id')),None)
        pattern=quality(last,previous,atr_value,mother)
        product['mother_id']=pattern['mother_id']
        pattern['related_zone_ids']=[z.zone_id for z in zones if z.lower<=last.high and z.upper>=last.low]
        pattern['preceding_regime']=struct['structure_state']
        m5p=confirmed_pivots(m5,received,asof,self.params['pivot_k'],self.params['leg_min_atr']) if m5 else []
        candidate=candidate or {}
        hypotheses=product.setdefault('hypotheses',{})
        if candidate.get('candidate_evidence_id'):
            hypotheses.setdefault(candidate['candidate_evidence_id'],dict(original=dict(candidate),last=None))
        hypothesis_changes=[]
        for item in hypotheses.values():
            original=item['original']
            if original.get('bar_eob') and clock(original['bar_eob'])>=trigger.eob:continue
            latest=observe(original,trigger,asof.isoformat(),item['last'])
            if latest!=item['last']:hypothesis_changes.append(latest)
            item['last']=latest
        room=available_room(candidate.get('direction'),candidate.get('entry'),candidate.get('stop'),candidate.get('multiplier'),
                            zones,asof,coverage=completeness=='COMPLETE_CLOSED_PREFIX' and receipt_complete and bool(current),
                            quote_known=candidate.get('quote_known',False),costs=candidate.get('costs'))
        required=[received.get(b.identity) for b in m15]
        result=dict(schema_version=SCHEMA,policy_version=POLICY,parameters_sha256=self.parameter_hash,mode='SHADOW',
            symbol=symbol,csymbol=csymbol,frequency='900s',trading_day=trading_day,session_id=session_id,
            source_bar_ids=[b.identity for b in m15],source_prefix_sha256=source_prefix_sha256,
            occurred_at=last.eob.isoformat(),confirmed_at=last.eob.isoformat(),
            first_received_at=received.get(last.identity),computed_at=asof.isoformat(),
            known_at=asof.isoformat() if all(required) else None,asof_at=asof.isoformat(),watermark=last.eob.isoformat(),
            completeness=completeness,event_id=key,episode_id=None,candidate_evidence_id=candidate.get('candidate_evidence_id'),
            status='SHADOW_ONLY',reason_codes=[] if all(required) else ['RECEIVE_CLOCK_UNKNOWN'],terminal_at=None,
            structure=struct,m5_structure=structure(m5p,m5[-1].close,tick) if m5 else dict(structure_state='UNKNOWN'),
            new_pivots=new_pivots,pivot_membership=sorted(current_ids),zones=[z.row() for z in zones],
            pivot_membership_changes=membership_changes,
            zone_lifecycle=dict(zone_status),
            mature_range=box,short_box=short,location=position(candidate.get('entry') or last.close,box,self.params),
            pattern=pattern,m5_pattern=None if not m5 else quality(m5[-1],m5[-2] if len(m5)>1 else None,atr_value),
            breakout_changes=episodes,touches=dict(product['touches']),room=room,
            breakout_frequency=trigger.frequency,breakout_bar_id=trigger.identity,
            invalidation=hypothesis(candidate.get('path'),candidate.get('direction'),candidate.get('entry_policy_proof'),struct),
            original_hypothesis_observations=hypothesis_changes,
            original_plan=candidate,execution_changed=False,trade_api_called=False)
        self.state.data['emitted'][key]=asof.isoformat()
        self.state.data.setdefault('pending_results',{})[key]=result
        self.state.save()
        return result
