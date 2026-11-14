"""Isolated production clock-process smoke test. No SDK, network or orders."""
from pathlib import Path
import argparse
import json
import multiprocessing
import os
import sys
import tempfile


def child(source, channel):
    os.environ.pop('GM_TOKEN',None);os.environ.pop('GM_STRATEGY_ID',None)
    sys.path.insert(0,str(source))
    from futures_trend_quant.live import paper_clock_health as ch
    from futures_trend_quant.live.paper_clock_probe import POLICY
    def synthetic(s):return dict(source=s,offset_seconds=.01,rtt_seconds=.01,stratum=2)
    ch.probe=synthetic
    class Done(Exception):pass
    ch.time.sleep=lambda delay:(_ for _ in ()).throw(Done())
    with tempfile.TemporaryDirectory(prefix='r15_clock_no_sdk_') as directory:
        root=Path(directory);ev=root/'evidence';ev.mkdir()
        (ev/'run_manifest.json').write_text(json.dumps(dict(run_id='SYNTHETIC')))
        try:ch.clock_process(ev,'SYNTHETIC','SYNTHETIC',ch.SAMPLER_POLICY,ch.RETRY_POLICY,POLICY)
        except Done:pass
        cfg=dict(root=str(root),evidence_dir='evidence',clock_policy_version=ch.VERSION,
                 clock_sampler_policy=ch.SAMPLER_POLICY,clock_probe_policy=POLICY,config_sha256='SYNTHETIC')
        health=ch.health(cfg)
        assert health['status']=='HEALTHY' and health['reason'] is None
        proof=json.loads((ev/'clock_health.local.json').read_text())
        assert proof['probe_policy']==POLICY and len(proof['samples'])==2
        channel.put(dict(passed=True,python=sys.version,clock_process='ACTUAL',
                         spawn=True,probe_policy=POLICY,gm_sdk_imported='gm.api' in sys.modules,
                         network=False,orders=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    ctx=multiprocessing.get_context('spawn');channel=ctx.Queue();process=ctx.Process(target=child,args=(a.source,channel));process.start();process.join(60)
    if process.is_alive():process.terminate();process.join();raise RuntimeError('R15_TEST_CHILD_TIMEOUT')
    if process.exitcode:raise RuntimeError('R15_TEST_CHILD_FAILED')
    result=channel.get(timeout=2);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result),encoding='utf8');print(json.dumps(result))
