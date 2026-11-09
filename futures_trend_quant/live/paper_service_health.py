"""Versioned rest/flat quarantine; never clear a persisted pause or risk halt.

Only connectivity/slow reads can be transient, born from an actual fresh flat
snapshot away from the next open. A new account proof is mandatory to recover.
The same run-bound, short-lived entry gate is checked by the native writer.
"""
from datetime import datetime,timezone,timedelta
from pathlib import Path
import json
from .paper_clock_health import atomic,health as clock_health

POLICY='REST_FLAT_SERVICE_QUARANTINE_V1_20261109'
REQUEST_POLICY='BOUND_PENDING_ENTRY_V1_20261110'
PREOPEN_SECONDS=600  # Existing user-required ten-minute preopen readiness boundary.
PROOF_SECONDS=3      # Existing full-account/entry freshness contract, unchanged.


def dt(value):
    value=value if isinstance(value,datetime) else datetime.fromisoformat(str(value))
    if value.tzinfo is None:raise ValueError('service clock timezone unknown')
    return value


class ServiceProofError(ValueError):
    def __init__(self,code,details):
        self.details=dict(details,failed_condition=code)
        super().__init__('SERVICE_RECOVERY_PENDING:'+code)


def require_service(config,now=None,request=None):
    if not config.get('clock_policy_version'):return
    root=Path(config.get('root','.'));ev=root/config['evidence_dir']
    details=dict(validated_at=dt(now or datetime.now(timezone.utc)).isoformat())
    try:
        row=json.loads((ev/'service_health.local.json').read_text(encoding='utf8'))
        manifest=json.loads((ev/'run_manifest.json').read_text(encoding='utf8'))
        age=(dt(now or datetime.now(timezone.utc))-dt(row['at'])).total_seconds()
        account_age=(dt(now or datetime.now(timezone.utc))-dt(row['account_verified_at'])).total_seconds()
        details.update(proof_at=row['at'],account_verified_at=row['account_verified_at'],
            proof_age_seconds=age,account_age_seconds=account_age,entry_gate_ready=row.get('entry_gate_ready'),
            request_id=None if request is None else request.get('request_id'),
            pending_entry_request=row.get('pending_entry_request'),run_id=row.get('run_id'))
        checks=[('POLICY',row['policy']==POLICY and manifest.get('service_policy_version')==POLICY),
            ('RUN',row['run_id']==manifest['run_id']),('PROFILE',row['config_sha256']==config['config_sha256']),
            ('PROOF_AGE',0<=age<=PROOF_SECONDS),('ACCOUNT_AGE',0<=account_age<=PROOF_SECONDS)]
        for code,ok in checks:
            if not ok:raise ServiceProofError(code,details)
        if row.get('entry_gate_ready') is True and not (request or {}).get('request_id'):return details
        # A WAL intent must block OTHER entries, but must not revoke its own
        # still-fresh final check. Only an explicitly bound, healthy writer
        # request can use this allowance. Unknown outcomes never qualify.
        permit=row.get('pending_entry_request')
        if row.get('entry_gate_ready') is not True and (row.get('request_policy')!=REQUEST_POLICY
                or not request or not permit or request.get('request_id')!=permit.get('request_id')):
            raise ServiceProofError('ENTRY_GATE_CLOSED',details)
        if (row.get('entry_gate_ready') is not True and row.get('service_ready') is not True) or row.get('active') is not None:
            raise ServiceProofError('SERVICE_NOT_READY',details)
        state=json.loads((root/config['state_dir']/'execution.json').read_text(encoding='utf8'))
        intent=state.get('intents',{}).get(request['request_id'])
        if (state.get('config_sha256')!=config['config_sha256'] or state.get('pause_new') or state.get('halt_reason')):
            raise ServiceProofError('RISK_STATE',details)
        if not intent or not pending_entry(intent):raise ServiceProofError('REQUEST_STATE',details)
        if any(i.get('cancel_outcome_unknown') for i in state['intents'].values()):
            raise ServiceProofError('UNKNOWN_CANCEL',details)
        if any(k!=request['request_id'] and i.get('status') not in (3,5,8,12) for k,i in state['intents'].items()):
            raise ServiceProofError('OTHER_ACTIVE_INTENT',details)
        if any(intent.get(k)!=request.get(k) for k in ('symbol','side','price','stop','path','core_evidence_id')) or int(request.get('position_effect',0))!=1:
            raise ServiceProofError('REQUEST_IDENTITY',details)
        if permit and (intent.get('evidence_id')!=permit.get('evidence_id') or intent.get('symbol')!=permit.get('symbol')):
            raise ServiceProofError('PERMIT_IDENTITY',details)
        return details
    except ServiceProofError:raise
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise ServiceProofError('PROOF_UNREADABLE',details) from exc


def pending_entry(intent):
    return (intent.get('effect')==1 and intent.get('status')==10 and not intent.get('order_id')
        and not intent.get('filled') and intent.get('native_submit_invoked') in (None,False)
        and not intent.get('submit_outcome_unknown') and not intent.get('cancel_outcome_unknown')
        and intent.get('execution_stage')!='SUBMIT_OUTCOME_UNKNOWN' and not intent.get('local_reject_reason'))


class ServiceHealth:
    def __init__(self,runner):
        self.r=runner;self.active=None;self.completed=None;self.sequence=0;self.last_write=None

    def context(self,now,reference=None):
        r=self.r;e=r.engine;now=dt(now);ref=dt(reference or now)
        calendars=getattr(r.calendar,'contracts',{})
        known=len(calendars)==5 and set(calendars)==set(r.records)
        closed=known and not any(c.market_open(now) for c in calendars.values())
        windows=[a for record in r.records.values() for a,b in record['trade_windows'] if a>now.timestamp()]
        next_open=min(windows) if windows else None
        age=None if not e or not e.last_snapshot else (ref-dt(e.last_snapshot)).total_seconds()
        positions=[] if not e or not e.snapshot else e.snapshot['positions']
        broker_active=[] if not e or not e.snapshot else e.snapshot['active']
        uncertain=not e or bool(e.active()) or any(x.get('cancel_outcome_unknown') for x in e.s['intents'].values())
        exposed=any(int(p.get('volume',0))>0 for p in positions) or bool(broker_active)
        fresh=bool(e and e.snapshot is not None and age is not None and 0<=age<=PROOF_SECONDS)
        flat=fresh and not exposed and not uncertain
        return dict(session='CLOSED' if closed else 'OPEN' if known else 'UNKNOWN',
            next_open=None if next_open is None else datetime.fromtimestamp(next_open,timezone.utc).isoformat(),
            preopen_due=next_open is None or next_open-now.timestamp()<=PREOPEN_SECONDS,
            account_verified_at=None if not e else e.last_snapshot,account_fresh=fresh,flat_confirmed=flat,
            exposed=exposed,uncertain_intents=uncertain,pause_new=True if not e else e.s['pause_new'],
            halt_reason='' if not e else e.s['halt_reason'])

    def fault(self,lane,state,now=None,code=None,reference=None):
        now=dt(now or datetime.now(timezone.utc));ctx=self.context(now,reference)
        if self.active is None:
            self.sequence+=1
            self.active=dict(id=self.r.run_id+':service:'+str(self.sequence),first_seen=now.isoformat(),
                last_seen=now.isoformat(),lanes={},observed_lanes={},codes={},notified=False,escalated=False,
                transient=(lane in ('market','reader') and state not in ('PROCESS_EXITED','TRANSPORT_CLOSED','RPC_FAILED')
                    and ctx['session']=='CLOSED' and not ctx['preopen_due'] and ctx['flat_confirmed']
                    and not ctx['pause_new'] and not ctx['halt_reason']),birth=ctx)
            self.r.event('SERVICE_EPISODE_STARTED',episode_id=self.active['id'],transient=self.active['transient'],context=ctx)
        ep=self.active;changed=ep['lanes'].get(lane)!=state
        ep['lanes'][lane]=state;ep['last_seen']=now.isoformat()
        ep['observed_lanes'][lane]=state
        if code is not None:ep['codes'][str(code)]=ep['codes'].get(str(code),0)+1
        if changed:self.r.event('SERVICE_EPISODE_LANE',episode_id=ep['id'],lane=lane,state=state)
        if (lane=='host_trade' or state in ('PROCESS_EXITED','TRANSPORT_CLOSED','RPC_FAILED')
                or ctx['session']!='CLOSED' or ctx['preopen_due'] or ctx['exposed'] or ctx['uncertain_intents']
                or ctx['halt_reason'] or ctx['pause_new']):ep['transient']=False
        if not ep['transient'] and self.r.engine and not self.r.engine.s['pause_new']:
            self.r.engine.pause_entries('SERVICE_INCIDENT_REQUIRES_REVIEW',now,'SERVICE_HEALTH')
        self.publish(now,force=changed or code is not None)

    def ready_facts(self,now,allow_local_pending=False):
        r=self.r;e=r.engine;ctx=self.context(now)
        products=getattr(r,'products',{})
        return bool(e and e.ready and ctx['account_fresh'] and (allow_local_pending or not ctx['uncertain_intents'])
            and len(products)==5 and all(p['ready'] and not p['frozen'] and not p['fault'] for p in products.values())
            and set(getattr(r,'tick_subscriptions',set()))==set(r.records)
            and all(getattr(r,'platform_connections',{}).get(k,{}).get('status')=='CONNECTED' for k in ('market','host_trade'))
            and not clock_health(r.c).get('reason'))

    def update(self,now):
        now=dt(now);r=self.r;e=r.engine
        if not e:return
        for lane in ('market','host_trade'):
            state=getattr(r,'platform_connections',{}).get(lane,{}).get('status')
            if state and state!='CONNECTED':self.fault(lane,state,now)
            elif state=='CONNECTED' and self.active:self.active['lanes'].pop(lane,None)
        reader=r.transport.reader;lane=reader.health()
        if lane['state'] in ('PROCESS_EXITED','TRANSPORT_CLOSED','SLOW_PENDING'):
            self.fault('reader',lane['state'],now,reference=lane.get('sent_at'))
        elif lane.get('last_reply_ok') is False:
            reason=lane.get('last_reason')
            self.fault('reader','READ_SERVICE_FAILED' if reason=='SIM_RPC_STATUS_1013' else 'RPC_FAILED',now,
                reference=lane.get('last_sent_at'))
        elif self.active and e.ready and self.context(now)['account_fresh']:
            self.active['lanes'].pop('reader',None)
        ep=self.active
        if ep:
            ep['reader_request']=lane
            ctx=self.context(now)
            if ep['transient'] and (ctx['session']!='CLOSED' or ctx['preopen_due'] or ctx['exposed']
                    or ctx['uncertain_intents'] or ctx['halt_reason'] or ctx['pause_new']):
                ep['transient']=False
                if not e.s['pause_new']:e.pause_entries('SERVICE_NOT_RECOVERED_BEFORE_OPEN_OR_RISK',now,'SERVICE_HEALTH')
                ctx=self.context(now)
            urgent=not ep['transient']
            if not ep['notified'] and (urgent or (now-dt(ep['first_seen'])).total_seconds()>=2):
                title='休市期间服务波动·临时阻断新开' if ep['transient'] else '服务连接异常·已停开待核验'
                self.send(ep,title,now,'warning' if ep['transient'] else 'critical',ctx)
                ep['notified']=True;ep['escalated']=urgent
            elif ep['notified'] and urgent and not ep['escalated']:
                self.send(ep,'服务故障升级·已锁存停开',now,'critical',ctx,suffix='escalated');ep['escalated']=True
            new_account=(e.last_snapshot and dt(e.last_snapshot)>dt(ep['first_seen'])
                and lane.get('last_sent_at') and dt(lane['last_sent_at'])>=dt(ep['first_seen']))
            if not ep['lanes'] and new_account and self.ready_facts(now):
                # Transient recovery requires NEW flat truth. Persisted pauses
                # and risk halts are never cleared, regardless of connectivity.
                if not ep['transient'] or ctx['flat_confirmed']:
                    ep['recovered_at']=now.isoformat();ep['pause_retained']=bool(e.s['pause_new'] or e.s['halt_reason'])
                    self.r.event('SERVICE_EPISODE_RECOVERED',episode_id=ep['id'],pause_retained=ep['pause_retained'])
                    if ep['notified']:
                        self.send(ep,'服务已恢复·账户仍暂停新开' if ep['pause_retained'] else '服务已恢复·休市临时阻断解除',
                            now,'warning' if ep['pause_retained'] else 'success',ctx,suffix='recovered')
                    self.completed=ep;self.active=None
        self.publish(now)

    def send(self,ep,title,now,severity,ctx,suffix='opened'):
        def show(value):return 'UNKNOWN' if value is None else dt(value).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d  %H:%M:%S')
        state='锁存停开，须受控恢复' if ctx['pause_new'] or ctx['halt_reason'] else '仅临时服务阻断；开市仍须全部原门槛'
        account='新鲜核验空仓' if ctx['flat_confirmed'] else '已有敞口' if ctx['exposed'] else '当前待重新核验'
        request=ep.get('reader_request') or {};age=request.get('request_age_seconds')
        self.r.notice(ep['id']+':'+suffix,title,[
            '交易小节 '+ctx['session']+'；下一连续交易 '+show(ctx['next_open']),
            '账户 '+account+'；最近账户事实 '+show(ctx['account_verified_at']),
            '触发通道 '+str(ep['observed_lanes'])+'；当前待恢复 '+str(ep['lanes'])+'；平台代码累计 '+str(ep['codes']),
            '只读请求 '+str(request.get('op') or '当前无在途请求')+'；在途秒数 '+('无' if age is None else format(age,'.2f')),
            '开始 '+show(ep['first_seen'])+'；'+state,
            '保留唯一写入者；未知提交/撤单不重发；退出义务持续，不以服务恢复声称已经平仓。'],severity=severity,priority=0 if severity=='critical' else 3)

    def publish(self,now,force=False):
        r=self.r
        service_ready=bool(self.active is None and r.engine and not r.engine.s['pause_new']
            and not r.engine.s['halt_reason'] and self.ready_facts(now,allow_local_pending=True))
        permit=None
        active=[] if not r.engine else r.engine.active()
        writer=getattr(getattr(r,'transport',None),'writer',None)
        lane=writer.health() if writer is not None else {}
        queue_state=getattr(writer,'describe_busy',lambda:{})() if writer is not None else {}
        waiting=queue_state.get('waiting',[])
        current=queue_state.get('current') or {}
        own_request=bool(len(active)==1 and (
            (lane.get('state')=='PENDING' and lane.get('op')=='submit' and lane.get('rpc_id')==active[0]['id'])
            or (lane.get('state') in ('IDLE','PENDING') and current.get('op')=='submit'
                and current.get('rpc_id')==active[0]['id'] and lane.get('rpc_id') in (None,active[0]['id']))
            or (lane.get('state') in ('IDLE','PENDING') and len(waiting)==1
                and waiting[0].get('op')=='submit' and waiting[0].get('rpc_id')==active[0]['id']
                and lane.get('rpc_id') in (None,active[0]['id']))))
        if (service_ready and len(active)==1 and pending_entry(active[0])
                and not any(i.get('cancel_outcome_unknown') for i in r.engine.s['intents'].values())
                and own_request):
            permit=dict(request_id=active[0]['id'],evidence_id=active[0].get('evidence_id'),symbol=active[0]['symbol'])
        ready=bool(self.active is None and r.engine and not r.engine.s['pause_new']
            and not r.engine.s['halt_reason'] and self.ready_facts(now))
        if (not force and self.last_write and (now-self.last_write).total_seconds()<.5
                and getattr(self,'state',{}).get('entry_gate_ready')==ready
                and getattr(self,'state',{}).get('pending_entry_request')==permit):return
        self.last_write=now
        self.state=dict(policy=POLICY,at=now.isoformat(),config_sha256=r.c['config_sha256'],run_id=r.run_id,
            entry_gate_ready=ready,service_ready=service_ready,pending_entry_request=permit,request_policy=REQUEST_POLICY,
            account_verified_at=None if not r.engine else r.engine.last_snapshot,
            active=self.active,last_completed=self.completed)
        atomic(r.evidence/'service_health.local.json',self.state)
