"""Research supervision model. Timeout does not revoke an in-flight native writer.

No process operations or account calls. Production integration remains unapproved.
"""
from dataclasses import dataclass,field


@dataclass
class RpcLease:
    current: dict | None = None
    stopped_confirmed: bool = False
    recon_required: bool = False
    unknown_orders: set = field(default_factory=set)
    exit_due_at: float | None = None
    flat_at: float | None = None
    timeline: list = field(default_factory=list)

    def send(self,rpc_id,op,at):
        if self.current or self.recon_required:raise ValueError('channel leased or recovery incomplete')
        self.current=dict(id=rpc_id,op=op,sent_at=at,soft_timeout=False)
        self.stopped_confirmed=False;self.timeline.append(dict(at=at,stage='SENT',op=op))

    def timeout(self,at,threshold=3):
        if self.current and at-self.current['sent_at']>=threshold:
            self.current['soft_timeout']=True;self.recon_required=True
            self.timeline.append(dict(at=at,stage='CALLER_TIMEOUT_NATIVE_STILL_OWNS_LEASE'))
            if self.current['op'] in ('submit','cancel'):self.unknown_orders.add(self.current['id'])

    def stop_requested(self,at):
        self.timeline.append(dict(at=at,stage='STOP_REQUEST_NOT_TERMINATION_PROOF'))

    def child_exit_proven(self,at):
        if self.current and self.current['op'] in ('submit','cancel'):self.unknown_orders.add(self.current['id'])
        self.stopped_confirmed=True;self.current=None;self.recon_required=True
        self.timeline.append(dict(at=at,stage='CHILD_EXIT_PROVEN_NOT_BROKER_ORDER_COMPLETION'))

    def reply(self,rpc_id,at):
        if not self.current or rpc_id!=self.current['id']:
            self.timeline.append(dict(at=at,stage='LATE_OR_FOREIGN_REPLY_QUARANTINED'));return
        self.current=None;self.recon_required=True
        self.timeline.append(dict(at=at,stage='REPLY_RECEIVED_RECON_REQUIRED'))

    def recover(self,*,fresh_truth,resolved_ids=(),manual_writer_handoff=False):
        self.unknown_orders.difference_update(resolved_ids)
        if self.current or self.unknown_orders or not fresh_truth:return False
        if self.stopped_confirmed and not manual_writer_handoff:return False
        self.recon_required=False;return True

    def exposure(self,at):
        return None if self.exit_due_at is None else max(0,(self.flat_at or at)-self.exit_due_at)
