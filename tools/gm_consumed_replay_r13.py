"""Local frozen-input first boot / second process / explicit migration audit.

No GM/TQ API, account, notifier, runtime checkpoint or production process mutation.
"""
from pathlib import Path
import argparse,hashlib,importlib.util,json,pickle,shutil,subprocess,sys,time


def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,x):Path(p).parent.mkdir(parents=True,exist_ok=True);Path(p).write_text(json.dumps(x,default=str,indent=2),encoding='utf8')


def worker(config_file,descriptor,output,old_source=None):
    if old_source:
        spec=importlib.util.spec_from_file_location('futures_trend_quant.live._frozen_r12_data',old_source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);SignalWorker=module.SignalWorker
    else:
        from futures_trend_quant.live.paper_data import SignalWorker
    c=pickle.loads(Path(config_file).read_bytes());w=SignalWorker(c,child=True);events=[]
    class Sink:
        def put(self,event):
            events.append(dict(kind=event[0],progress=event[1] if event[0] in ('CORE_READY','CORE_FAILED','WARMUP_FAILED') else None))
            if event[0] in ('CORE_READY','CORE_FAILED','WARMUP_FAILED'):w.stop=True
    w.outbox=Sink();w.inbox.put(('CALENDAR',c['replay_metadata']));w.inbox.put(('INITIAL_CACHE',read(descriptor)));began=time.monotonic();w._run()
    write(output,dict(events=events,seconds=time.monotonic()-began,pid=__import__('os').getpid(),python=sys.version,network_calls=0))


def normalize(value):
    if isinstance(value,dict):return {k:normalize(v) for k,v in value.items() if k not in ('first_evaluated_at','first_qualified_at')}
    if isinstance(value,list):return [normalize(v) for v in value]
    return value


def audit(client,repo,out):
    from futures_trend_quant.live.paper_config import load_paper_config
    from futures_trend_quant.live.paper_gm_inputs import POLICY
    rt=client/'_strategy_runtime';qa_path=rt/'evidence/paper_observation_five/reviews/20261112_r12_restart_failure/freeze_20261112T052032Z/qa.local.json'
    qa=read(qa_path);frozen={str(Path(x['source']).resolve()).lower():qa_path.parent/x['copy'] for x in qa['files']}
    def exact(path):
        key=str(Path(path).resolve()).lower();p=frozen[key];row=next(x for x in qa['files'] if str(Path(x['source']).resolve()).lower()==key)
        if sha(p)!=row['sha256']:raise ValueError('FROZEN_PREFIX_CHANGED')
        return p
    base=load_paper_config(rt/'config/paper_observation_five.local.json',root=rt)
    old_release=repo/'evidence/releases/startup_r12_20261112';old_source=old_release/'client/_strategy_runtime/futures_trend_quant/live/paper_data.py'
    results=[];out.mkdir(parents=True,exist_ok=False)
    for product in qa['products']:
        slug=product['csymbol'];cs=slug.replace('_','.');work=out/slug;work.mkdir()
        original=rt/'state/paper_observation_five/products'/slug/'core_checkpoint.json';actual=read(exact(original))
        incoming=next(x for x in product['actual_initial_caches'] if 'T045' in x['file'])
        def branch(name,policy=False):
            root=work/name;state=root/'state';state.mkdir(parents=True);cache=root/'data/cache/paper_market';cache.mkdir(parents=True)
            saved=read(old_release/'rollback/state_at_install/products'/slug/'core_checkpoint.json')
            for desc,key in ((saved['seed_cache'],'seed'),(incoming,'incoming')):
                path=cache/(key+'.parquet');shutil.copyfile(exact(desc.get('path',desc.get('file'))),path)
                if sha(path)!=desc['sha256']:raise ValueError('CACHE_COPY_CHANGED')
                if key=='seed':saved['seed_cache']=dict(desc,path=str(path))
                else:initial=dict(path=str(path),sha256=sha(path),symbol=actual['seed_cache']['symbol'],cutoff='2026-11-12T12:54:00+08:00')
            journal=state/'gm_core_m1.jsonl';shutil.copyfile(exact(original.parent/'gm_core_m1.jsonl'),journal)
            if saved.get('gm_input_prefix'):saved['gm_input_prefix']['path']=str(journal)
            write(state/'core_checkpoint.json',saved);write(root/'incoming.json',initial)
            c=dict(base,root=str(root),state_dir=str(state),evidence_dir=str(root/'evidence'),csymbol=cs,
                replay_metadata=read(rt/'evidence/paper_observation_five/products'/slug/'market_metadata.local.json'))
            if policy:c['gm_consumed_input_policy']=POLICY
            (root/'config.pkl').write_bytes(pickle.dumps(c));return root
        old=branch('old');new=branch('new',True)
        def run(root,label,old=False):
            args=[sys.executable,'-X','utf8',str(Path(__file__).resolve()),'worker','--config',str(root/'config.pkl'),
                '--descriptor',str(root/'incoming.json'),'--output',str(root/(label+'.json'))]
            if old:args+=['--old-source',str(old_source)]
            proc=subprocess.run(args,cwd=repo,capture_output=True,text=True,timeout=600)
            if proc.returncode:raise RuntimeError('REPLAY_CHILD_FAILED:'+proc.stderr[-1000:])
            return read(root/(label+'.json'))
        old_first=run(old,'first',True);old_cp=read(old/'state/core_checkpoint.json');old_second=run(old,'second',True)
        new_first=run(new,'first');new_cp=read(new/'state/core_checkpoint.json');new_second=run(new,'second')
        restarted=read(new/'state/core_checkpoint.json')
        assert old_first['events'][-1]['kind']=='CORE_READY'
        assert old_second['events'][-1]['kind']=='CORE_FAILED' and old_second['events'][-1]['progress']['detail']=='GM_CHECKPOINT_USED_INPUT_PREFIX_MISSING'
        assert new_first['events'][-1]['kind']==new_second['events'][-1]['kind']=='CORE_READY'
        assert normalize(old_cp['runtime'])==normalize(new_cp['runtime'])==normalize(actual['runtime'])
        assert normalize(new_cp['runtime'])==normalize(restarted['runtime']) and actual['last_eob']==restarted['last_eob']
        # A migrated checkpoint retains every actual runtime clock and consumed ID.
        migrated=dict(actual,seed_cache=new_cp['seed_cache'],gm_input_prefix=new_cp.get('gm_input_prefix'),gm_input_cache=new_cp['gm_input_cache'])
        write(new/'state/core_checkpoint.json',migrated);migrated_run=run(new,'migrated')
        assert migrated_run['events'][-1]['kind']=='CORE_READY'
        after=read(new/'state/core_checkpoint.json');assert after['runtime']==actual['runtime'] and after['last_eob']==actual['last_eob']
        proof=new_cp['gm_input_cache']
        results.append(dict(csymbol=cs,checkpoint_sha256=sha(exact(original)),consumed_initial_sha256=incoming['sha256'],
            old_second_failed=True,new_second_ready=True,migration_ready=True,runtime_preserved_exact=True,
            first_boot_equal_except_actual_evaluation_clocks=True,excluded_clock_fields=['first_evaluated_at','first_qualified_at'],
            gm_input_cache=proof,old_first=old_first,new_first=new_first,new_second=new_second,migrated=migrated_run))
        write(out/'audit.local.json',dict(products=results,orders=0,originals_untouched=True,
            source_sha256_lf={rel:hashlib.sha256((repo/rel).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
                for rel in ('futures_trend_quant/live/paper_data.py','futures_trend_quant/live/paper_gm_inputs.py')},
            old_source_sha256_lf=hashlib.sha256(old_source.read_bytes().replace(b'\r\n',b'\n')).hexdigest()))
        print(json.dumps(dict(csymbol=cs,passed=True,new_second_seconds=new_second['seconds'])),flush=True)
    return results


if __name__=='__main__':
    # Running as a file gives actual subprocesses the same repository import root.
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['worker','audit']);p.add_argument('--config');p.add_argument('--descriptor');p.add_argument('--output',required=True);p.add_argument('--old-source');p.add_argument('--client');a=p.parse_args()
    if a.mode=='worker':worker(a.config,a.descriptor,a.output,a.old_source)
    else:audit(Path(a.client),Path(__file__).resolve().parents[1],Path(a.output))
