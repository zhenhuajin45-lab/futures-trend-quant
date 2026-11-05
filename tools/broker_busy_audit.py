"""Read-only production evidence + no-network baseline reproduction. Never runs a broker."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from datetime import datetime
import argparse,hashlib,json,queue,statistics,tempfile,time
import pandas as pd


def offline_probe(folder):
    from futures_trend_quant.live import paper_five as five
    from futures_trend_quant.live.paper_broker import PaperEngine
    from futures_trend_quant.live.paper_calendar import PaperCalendar
    from futures_trend_quant.live.paper_transport import SimTransport
    now=pd.Timestamp('2026-11-05T10:00:00+08:00');account='00000000-0000-0000-0000-000000000001'
    def setup(path):
        c=dict(account_id=account,config_sha256='OFFLINE_SYNTHETIC',state_dir=str(path/'state'),evidence_dir=str(path/'evidence'),quote_max_age_seconds=3,max_spread_ticks=4,open_wait_seconds=10,signal_max_age_seconds=30,max_positions=2,risk_pct=.0025,margin_usage_pct=.2,daily_loss_pct=.01,observation_drawdown_pct=.05)
        calendar=PaperCalendar(['2026-11-04','2026-11-05','2026-11-06','2026-11-09'],[dict(start='9:00',end='10:15'),dict(start='10:30',end='11:30'),dict(start='13:30',end='15:00')])
        t=SimTransport.__new__(SimTransport);t.account=account;t.jobs=queue.Queue();t.results=queue.Queue();t.current=None;t.started=None;t.closed=False
        e=PaperEngine(c,calendar,t);snapshot=dict(cash=dict(account_id=account,nav=1000000.,available=999900.,cum_commission=0),positions=[],orders=[],active=[],fills=[])
        e.apply_snapshot(snapshot,now);e.s['connectivity_passed']=True
        e.quote('DCE.m2701',dict(created_at=str(now),bid=100.,ask=101.,tick=1.,bid_size=50,ask_size=50,last=101.),now)
        signal=dict(symbol='DCE.m2701',direction='bull',evidence_id='OFFLINE_EVIDENCE',signal_at=str(now),anchor=99.,atr=2.,multiplier=10.,margin_ratio=.07,rules=dict(stop_buffer_atr=.6,stop_min_atr=1.5,stop_max_atr=6),path='D',m15_index=1234)
        return e,t,snapshot,signal
    folder=Path(folder);e,t,snapshot,signal=setup(folder/'busy');flow=[];decisions=[]
    # Exactly the live transport busy predicate, with a snapshot rather than an order in flight.
    t.current=dict(op='snapshot',id='SYNTHETIC_SNAPSHOT');t.started=time.monotonic()
    t.results.put((dict(op='snapshot'),dict(ok=True,data=snapshot)))
    drain=t.drain
    def receive():flow.append('DRAIN_SNAPSHOT');t.current=None;t.started=None;return drain()
    t.drain=receive
    r=five.FiveRunner.__new__(five.FiveRunner);r.last_loop=time.monotonic();r.max_gap=0;r.products={};r.quote_worker=SimpleNamespace(is_alive=lambda:True)
    r.engine=e;r.transport=t;r.readonly=False;r.c=e.c;r.calendar=SimpleNamespace(contracts={});r.last_poll=time.monotonic();r.last_report=time.monotonic();r.fill_notified=set();r.last_problem=None
    r.notice=Mock();r.event=Mock();r.pump_notifications=Mock();r.market_events=Mock();sources=[signal]
    def evaluate(current):
        if sources:flow.append('EVALUATE_SIGNAL');decisions.append(e.open(sources.pop(),current))
    r.core_events=evaluate
    with patch.object(five,'pd',SimpleNamespace(Timestamp=SimpleNamespace(now=lambda **kwargs:now))):
        r.cycle();r.cycle()
    lost=dict(first_decision=decisions[0],signal_evaluations=len(decisions),flow=flow,intents_after_idle_cycle=len(e.s['intents']),transport_jobs_after_idle_cycle=t.jobs.qsize(),account_ready_after_snapshot=e.ready)
    # Manual offline reevaluation proves the synthetic price/risk were eligible.
    valid_after_release=e.open(signal,now+pd.Timedelta(milliseconds=200))
    risk,rt,_,rs=setup(folder/'risk');rs['multiplier']=1000.
    risk_result=risk.open(rs,now)
    return dict(scope='SYNTHETIC_NO_NATIVE_API_NO_NETWORK',baseline_defect_reproduced=lost['first_decision']==(False,'BROKER_BUSY') and lost['intents_after_idle_cycle']==0,
        local_query_busy_signal_lost=lost,eligible_signal_would_pass_when_reevaluated=bool(valid_after_release[0]),synthetic_queued_submit_count=t.jobs.qsize(),
        unchanged_one_lot_risk_guard=risk_result[1],over_budget_native_submit_count=rt.jobs.qsize(),native_api_calls=0)


def live_evidence(client):
    root=Path(client)/'_strategy_runtime';ev=root/'evidence/paper_observation_five';data={};index=[]
    for name in ('signals.jsonl','position_recon.jsonl','order_events.jsonl'):
        raw=(ev/name).read_bytes();lines=raw.decode('utf8').splitlines();rows=[]
        for n,line in enumerate(lines):
            try:rows.append(json.loads(line))
            except ValueError:
                if n!=len(lines)-1:raise
        data[name]=rows;index.append(dict(file=name,bytes_read=len(raw),sha256=hashlib.sha256(raw).hexdigest(),scope='EXACT_READ_PREFIX'))
    dt=lambda x:datetime.fromisoformat(str(x).replace('Z','+00:00'))
    samples=[]
    for s in data['signals.jsonl']:
        if not (s.get('entry') or {}).get('accepted'):continue
        t=dt(s['order_evaluated_at']);before=[r for r in data['position_recon.jsonl'] if dt(r['at'])<=t];after=[r for r in data['position_recon.jsonl'] if dt(r['at'])>t]
        samples.append(dict(signal_at=s['bar_eob'],evaluation_at=str(t),csymbol=s['csymbol'],decision=s['paper_decision'],last_reconciled_at=before[-1]['at'] if before else None,
            next_reconciled_at=after[0]['at'] if after else None,seconds_until_next_recon=(dt(after[0]['at'])-t).total_seconds() if after else None,
            prior_active_orders=len(before[-1]['active']) if before else None,prior_positive_positions=sum(int(p['volume'])>0 for p in before[-1]['positions']) if before else None))
    night=[r for r in data['position_recon.jsonl'] if dt(r['at'])>=dt('2026-11-05T21:00:00+08:00')]
    gaps=[(dt(b['at'])-dt(a['at'])).total_seconds() for a,b in zip(night,night[1:])]
    native_failures=[r for r in data['order_events.jsonl'] if r.get('kind')=='RPC_FAILED' and dt(r['recorded_at'])>=dt('2026-11-05T21:00:00+08:00')]
    files=['futures_trend_quant/live/'+n for n in ('paper_broker.py','paper_transport.py','paper_five.py','paper_sim_bridge.py')];code=[]
    repo=Path(__file__).resolve().parents[1]
    for name in files:
        a=(root/name).read_bytes().replace(b'\r\n',b'\n');b=(repo/name).read_bytes().replace(b'\r\n',b'\n')
        code.append(dict(file=name,sha256_lf=hashlib.sha256(a).hexdigest(),matches_reviewed_source=a==b))
    return dict(samples=samples,night_recon_count=len(night),night_recon_interval_median_seconds=statistics.median(gaps) if gaps else None,
        night_native_rpc_failure_count=len(native_failures),rpc_op_and_roundtrip_logged_for_snapshot=False,
        exact_busy_request_op_for_historical_signal='NOT_RECORDED;SNAPSHOT_INFERRED_FROM_CALL_CHAIN',input_index=index,reviewed_runtime_sources=code)


def main():
    p=argparse.ArgumentParser();p.add_argument('--client',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    with tempfile.TemporaryDirectory(prefix='ftq_busy_audit_') as tmp:probe=offline_probe(Path(tmp))
    report=dict(at=pd.Timestamp.now(tz='Asia/Shanghai').isoformat(),scope='READ_ONLY_LIVE_EVIDENCE_AND_OFFLINE_REPRODUCTION',live=live_evidence(a.client),offline=probe,
        conclusion='LOCAL_EXECUTION_HANDOFF_DEFECT_NOT_VENDOR_REJECTION',live_code_modified=False,processes_restarted=False,old_signals_resubmitted=False)
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(report=str(a.output),defect_reproduced=probe['baseline_defect_reproduced'],samples=len(report['live']['samples']),vendor_rpc_failures=report['live']['night_native_rpc_failure_count']),ensure_ascii=False))


if __name__=='__main__':main()
