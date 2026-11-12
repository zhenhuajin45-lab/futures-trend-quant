"""Release only runtime health/quote/notice changes, retaining the V5 strategy.

Installs while stopped and flat. Resume is an explicit operator invocation,
single-use for this configuration, after the new host was verified while paused.
No process is started/stopped here; no order or clock adjustment API is called.
"""
from pathlib import Path
from datetime import datetime,timezone
import argparse
import json
import subprocess
import sys
from tools import paper_v5_release as v5
from tools import paper_core_v2_release as prior
from futures_trend_quant.live.paper_broker import atomic
from futures_trend_quant.live.paper_clock_health import VERSION,SOURCE_POLICY,require_clock,collect
from futures_trend_quant.live.paper_quote_feed import POLICY


def prepare(client,bundle):
    v5.prepare(client,bundle,entry=True)
    stage=bundle/'client';profile=stage/'_strategy_runtime/config/paper_observation_five.local.json'
    c=prior.read(profile);c.update(clock_policy_version=VERSION,quote_policy_version=POLICY)
    atomic(profile,c)
    result=subprocess.run([sys.executable,'-X','utf8',str(stage/'main.py'),'--self-check','--check-config'],capture_output=True,text=True,encoding='utf8',check=True)
    plan=prior.read(bundle/'release_plan.local.json')
    plan.update(new_config_sha256=prior.sha(profile),runtime_health_version='PAPER_RUNTIME_READINESS_V6_20261109',
        clock_policy_version=VERSION,clock_source_policy_version=SOURCE_POLICY,quote_policy_version=POLICY,self_check=json.loads(result.stdout.strip().splitlines()[-1]))
    atomic(bundle/'release_plan.local.json',plan)
    print(json.dumps(dict(prepared=True,main_sha256_lf=plan['new_main_sha256_lf'],config_sha256=plan['new_config_sha256'],active_modified=False)))


def install(client,bundle):
    plan=prior.read(bundle/'release_plan.local.json');gate=prior.read(bundle/'validation_gate.local.json')
    if plan.get('clock_source_policy_version')!=SOURCE_POLICY:raise ValueError('wrong clock source release')
    for field in ('clock_guards_passed','quote_chain_passed','candidate_terminal_passed','core_unchanged'):
        if gate.get(field) is not True:raise ValueError('missing runtime health release gate: '+field)
    if gate.get('config_sha256')!=plan['new_config_sha256']:raise ValueError('runtime validation profile mismatch')
    v5.install(client,bundle)
    p=client/'_strategy_runtime/state/paper_observation_five/execution.json';s=prior.read(p)
    s['pause_metadata']=dict(reason='REVIEWED_RUNTIME_V6_HANDOFF',source='EXPLICIT_RELEASE',at=datetime.now(timezone.utc).isoformat())
    atomic(p,s)


def resume(client,bundle,authorization=None):
    rt=client/'_strategy_runtime';ev=rt/'evidence/paper_observation_five';plan=prior.read(bundle/'release_plan.local.json')
    if (plan.get('clock_policy_version')!=VERSION or plan.get('quote_policy_version')!=POLICY
            or plan.get('clock_source_policy_version')!=SOURCE_POLICY):raise ValueError('wrong runtime release')
    # All lifecycle/account/source/notifier/facts and single-use gates remain in
    # prior.resume(). Current clock and actually acknowledged subscriptions add
    # two requirements; neither is an exemption for existing protections.
    c=prior.load_paper_config(rt/'config/paper_observation_five.local.json',root=rt)
    # No controller is running while an explicit operator takes a new clock
    # measurement. This is a real probe, never a timestamp refresh of old data.
    prior.flat_stopped(client)
    status=prior.read(ev/'status.json');feed=status.get('quote_feed') or {}
    from futures_trend_quant.live.paper_symbol_health import enabled as isolated
    if isolated(c) and not authorization:raise ValueError('fresh explicit recovery authorization required')
    from futures_trend_quant.live.paper_tq import external_product
    expected={p['symbol'] for cs,p in status['products'].items() if not isolated(c) or not external_product(c,cs)}
    if isolated(c):
        gm_pool=expected
        expected={p['symbol'] for cs,p in status['products'].items() if not external_product(c,cs)
            and status.get('symbol_entry_readiness',{}).get(p['symbol'],{}).get('market_proof',{}).get('ready') is True}
        subscriptions_ok=expected<=set(feed.get('subscribed',[]))<=gm_pool
    else:subscriptions_ok=set(feed.get('subscribed',[]))==expected and len(expected)==5
    if feed.get('policy')!=POLICY or not subscriptions_ok:
        raise ValueError('actual five-contract tick subscription acknowledgments required')
    manifest=prior.read(Path(c['evidence_dir'])/'run_manifest.json')
    clock=collect(c['config_sha256'],manifest['run_id'])
    if c.get('clock_sampler_policy'):clock.update(sampler_policy=c['clock_sampler_policy'],sampler_sequence=0)
    clock['measurement_context']='EXPLICIT_USER_AUTHORIZED_FLAT_STOPPED_RESUME'
    atomic(bundle/'operator_clock_check.local.json',clock)
    if clock['status']!='HEALTHY':raise ValueError('SYSTEM_CLOCK_UNTRUSTED')
    atomic(Path(c['evidence_dir'])/'clock_health.local.json',clock)
    require_clock(c)
    prior.resume(client,bundle,authorization=authorization)
    receipt=bundle/('controlled_resume.local.json' if authorization is None else 'controlled_resume_'+prior.hashlib.sha256(authorization['token'].encode()).hexdigest()[:16]+'.local.json')
    proof=prior.read(receipt)
    proof.update(runtime_health_version=plan['runtime_health_version'],scope='THIS_REVIEWED_RELEASE_ONLY_NO_FUTURE_AUTO_RESUME',
        authorization_basis=authorization['basis'] if authorization else 'USER_20261109_COMPREHENSIVE_FIX_TEST_RELEASE_FOR_NIGHT',clock_verified=True,clock_source_policy_version=SOURCE_POLICY,tick_subscriptions_verified=len(feed.get('subscribed',[])))
    atomic(receipt,proof)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','install','resume']);p.add_argument('--client',type=Path,required=True);p.add_argument('--bundle',type=Path,required=True);a=p.parse_args()
    dict(prepare=prepare,install=install,resume=resume)[a.mode](a.client.resolve(),a.bundle.resolve())
