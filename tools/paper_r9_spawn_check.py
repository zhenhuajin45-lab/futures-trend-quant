"""Actual Windows spawn + generated __mp_main__ bootstrap, no SDK/network."""
from pathlib import Path
import argparse,json,multiprocessing,os,sys,time


def child(main,output,channel):
    from multiprocessing.spawn import _fixup_main_from_path
    began=time.perf_counter();_fixup_main_from_path(str(main))
    assert 'gm.api' not in sys.modules and 'gmtrade.api' not in sys.modules
    bootstrap=time.perf_counter()-began
    # Execute real SDK-free targets through their exact secret-removal entry.
    from futures_trend_quant.live import paper_data,paper_clock_health
    captures={}
    class Probe:
        def __init__(self,*a,**k):pass
        def _run(self):captures['core_secret_absent']=not any(k in os.environ for k in ('GM_TOKEN','GM_STRATEGY_ID'))
    os.environ.update(GM_TOKEN='SYNTHETIC_NOT_A_CREDENTIAL',GM_STRATEGY_ID='SYNTHETIC')
    paper_data.SignalWorker=Probe;paper_data._signal_process({},None,None)
    class EndProbe(Exception):pass
    def collect(*a):
        captures['clock_secret_absent']=not any(k in os.environ for k in ('GM_TOKEN','GM_STRATEGY_ID'));raise EndProbe()
    os.environ.update(GM_TOKEN='SYNTHETIC_NOT_A_CREDENTIAL',GM_STRATEGY_ID='SYNTHETIC')
    paper_clock_health.collect=collect
    try:paper_clock_health.clock_process(output,'SYNTHETIC','SYNTHETIC')
    except EndProbe:pass
    assert all(captures.values()) and 'gm.api' not in sys.modules
    channel.put(dict(passed=True,spawn_bootstrap_seconds=bootstrap,gm_sdk_imported=False,**captures,
        target_work='PROBE_ONLY_NO_CORE_COMPUTE_OR_NTP_NETWORK',python=sys.version.split()[0]))


def main(source,output):
    ctx=multiprocessing.get_context('spawn');q=ctx.Queue();p=ctx.Process(target=child,args=(source,output,q));p.start()
    p.join(60)
    if p.is_alive():p.terminate();p.join();raise RuntimeError('R9_TEST_CHILD_TIMEOUT')
    if p.exitcode:raise RuntimeError('R9_TEST_CHILD_FAILED:'+str(p.exitcode))
    result=q.get(timeout=2);output.parent.mkdir(exist_ok=True,parents=True);output.write_text(json.dumps(result),encoding='utf8');print(json.dumps(result))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--main',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.main.resolve(),a.output.resolve())
