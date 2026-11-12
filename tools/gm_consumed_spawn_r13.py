"""Two real Windows-spawned production core workers, frozen sandbox inputs only."""
from pathlib import Path
import json,pickle,sys,time


def main(root,output):
    from futures_trend_quant.live.paper_data import SignalWorker
    c=pickle.loads((root/'config.pkl').read_bytes());proof=json.loads((root/'incoming.json').read_text());results=[]
    before=json.loads((Path(c['state_dir'])/'core_checkpoint.json').read_text())['runtime']
    for attempt in range(2):
        w=SignalWorker(c);w.thread.start();pid=w.thread.pid;started=time.monotonic()
        try:
            w.inbox.put(('CALENDAR',c['replay_metadata']));w.inbox.put(('INITIAL_CACHE',proof))
            while True:
                event=w.outbox.get(timeout=60)
                if event[0] in ('CORE_FAILED','WARMUP_FAILED'):raise RuntimeError(str(event))
                if event[0]=='CORE_READY':break
            after=json.loads((Path(c['state_dir'])/'core_checkpoint.json').read_text())['runtime']
            assert before==after
            results.append(dict(attempt=attempt+1,pid=pid,ready=True,seconds=time.monotonic()-started,runtime_exact=True))
        finally:
            # Only this isolated test child: no host, account, SDK or trading API.
            w.thread.terminate();w.thread.join(10)
            assert not w.thread.is_alive()
    assert results[0]['pid']!=results[1]['pid']
    receipt=dict(passed=True,actual_production_target='_signal_process',spawned_core_workers=results,
        actual_client_host_restart=False,network_or_orders=False,python=sys.version)
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(receipt),encoding='utf8');print(json.dumps(receipt))


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]));main(Path(sys.argv[1]),Path(sys.argv[2]))
