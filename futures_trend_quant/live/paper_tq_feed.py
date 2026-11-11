"""Owned TqSdk data sidecar. No broker, order, account or target-position API."""
from pathlib import Path
import argparse
import contextlib
import io
import json
import os
import time
import hashlib
from .paper_tq import POLICY, atomic,append, now_text, load_manifest, quote_values


def publish(config_path,run_id,spool,seconds=None):
    c=json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    root=Path(config_path).resolve().parents[1]
    from .paper_tq import sha,validate_config
    c.update(config_sha256=sha(config_path),root=str(root));validate_config(c,root)
    manifest=load_manifest(c,root);spool=Path(spool).resolve()
    expected=(root/c['evidence_dir']/'tq_feed'/run_id).resolve()
    if spool!=expected:raise ValueError('TQ_SPOOL_BOUNDARY')
    spool.mkdir(parents=True,exist_ok=False)
    # One creator for this host generation. A second process must not overwrite proof.
    lease=(spool/'publisher.lock').open('x',encoding='utf8');lease.write(str(os.getpid()));lease.flush()
    proof_path=spool.parent/'current_run.json'
    proof=dict(policy=POLICY,config_sha256=c['config_sha256'],run_id=run_id,pid=os.getpid(),
        contracts={cs:r['symbol'] for cs,r in manifest['products'].items()},status='STARTING',updated_at=now_text())
    atomic(proof_path,proof)
    health_path=spool/'health.jsonl';append(health_path,proof)
    try:
        # Optional SDK stays in its existing isolated interpreter. No credentials/logs in stdout.
        with open(os.devnull,'w',encoding='utf8') as quiet,contextlib.redirect_stdout(quiet),contextlib.redirect_stderr(quiet):
            from tqsdk import TqApi,TqAuth,TqSim
            from tools.supplemental_tq.credentials import authentication
            if __import__('tqsdk').__version__!='3.10.4':raise ValueError('TQ_SDK_VERSION_CHANGED')
            if __import__('pandas').__version__!='2.3.3':raise ValueError('TQ_PANDAS_VERSION_CHANGED')
            os.environ['TQ_AUTH_FILE']=str(root/c['tq_feed']['auth_file'])
            os.environ.pop('TQ_USER',None);os.environ.pop('TQ_PASSWORD',None)
            with TqApi(account=TqSim(),auth=TqAuth(*authentication()),debug=False,web_gui=False) as api:
                quotes={cs:api.get_quote(r['symbol']) for cs,r in manifest['products'].items()}
                mains={cs:api.get_quote('KQ.m@'+r['symbol'].split('.')[0]+'.'+''.join(x for x in r['symbol'].split('.')[1] if x.isalpha())) for cs,r in manifest['products'].items()}
                frames={cs:api.get_kline_serial(r['symbol'],60,data_length=1000) for cs,r in manifest['products'].items()}
                metadata={cs:json.loads((root/r['metadata']).read_text(encoding='utf8')) for cs,r in manifest['products'].items()}
                last_quotes={};bar_keys={};bar_records={};started=time.monotonic();last_publish=0
                while seconds is None or time.monotonic()-started<seconds:
                    stop=spool/'stop.request.json'
                    if stop.exists():
                        request=json.loads(stop.read_text(encoding='utf8'))
                        if request.get('run_id')!=run_id or request.get('config_sha256')!=c['config_sha256']:raise ValueError('TQ_STOP_REQUEST_IDENTITY')
                        break
                    stage='API_WAIT_UPDATE'
                    api.wait_update(deadline=time.time()+.5)
                    if time.monotonic()-last_publish<.2:continue
                    last_publish=time.monotonic()
                    all_ready=True;product_status={}
                    from .paper_symbol_health import enabled as isolated
                    for cs,q in quotes.items():
                        stage='SOURCE_METADATA'
                        try:
                            symbol=manifest['products'][cs]['symbol'];spec=metadata[cs]['spec']
                            if not q.datetime or not api.is_serial_ready(frames[cs]):all_ready=False;product_status[cs]=dict(ready=False,reason='SOURCE_NOT_READY',updated_at=now_text());continue
                            if float(q.price_tick)!=float(spec['price_tick']) or float(q.volume_multiple)!=float(spec['multiplier']):raise ValueError('TQ_STATIC_SPEC_MISMATCH')
                            stage='QUOTE'
                            raw={k:getattr(q,k) for k in ('datetime','last_price','bid_price1','ask_price1','bid_volume1','ask_volume1')}
                            raw_changed=cs not in last_quotes or last_quotes[cs]['raw']!=raw
                            changed=raw_changed or last_quotes[cs].get('main')!=mains[cs].underlying_symbol
                            if raw_changed:
                                last_quotes[cs]=dict(raw=raw,quote=quote_values(raw,spec,run_id),received_at=now_text())
                            last_quotes[cs]['main']=mains[cs].underlying_symbol
                            updated=now_text()
                            envelope=dict(policy=POLICY,config_sha256=c['config_sha256'],run_id=run_id,symbol=symbol,updated_at=updated,
                                rollover=not mains[cs].underlying_symbol or mains[cs].underlying_symbol!=symbol,
                                quote=last_quotes[cs]['quote'],received_at=last_quotes[cs]['received_at'])
                            stage='CLOSED_INPUT'
                            frame=frames[cs]
                            columns=['datetime','open','high','low','close','volume','close_oi']
                            key=hashlib.sha256(frame.iloc[:-1][columns].to_numpy().tobytes()).hexdigest()
                            if bar_keys.get(cs)!=key:
                                rows=[]
                                for raw in frame.iloc[:-1].to_dict('records'):
                                    if raw['datetime']>0:
                                        rows.append({k:int(raw[k]) if k=='datetime' else float(raw[k]) for k in columns})
                                previous=bar_records.get(cs,{})
                                delta=[r for r in rows if previous.get(r['datetime'])!=r]
                                if delta:
                                    append(spool/(cs.replace('.','_')+'.bars.jsonl'),dict(policy=POLICY,config_sha256=c['config_sha256'],run_id=run_id,
                                        symbol=symbol,updated_at=updated,known_at=updated,forming_bob_ns=int(frame.iloc[-1]['datetime']),rows=delta))
                                bar_records[cs]={r['datetime']:r for r in rows}
                                bar_keys[cs]=key
                            if changed:append(spool/(cs.replace('.','_')+'.quotes.jsonl'),envelope)
                            product_status[cs]=dict(ready=True,reason=None,updated_at=now_text())
                        except (ValueError,KeyError,TypeError,OverflowError) as exc:
                            if not isolated(c):raise
                            all_ready=False
                            code=str(exc) if str(exc).startswith('TQ_') and str(exc).replace('_','').isalnum() else 'SOURCE_INPUT_REJECTED'
                            product_status[cs]=dict(ready=False,reason=code,error_type=type(exc).__name__,stage=stage,updated_at=now_text())
                    if isolated(c):proof.update(product_status=product_status,progress_scope='PUBLISHER_LOOP_NOT_ALL_SYMBOLS_READY',symbol_market_policy=c['symbol_market_policy'])
                    proof.update(status='RUNNING' if all_ready or isolated(c) else 'STARTING',updated_at=now_text(),
                        main_mapping={cs:q.underlying_symbol for cs,q in mains.items()})
                    append(health_path,proof)
        proof.update(status='STOPPED',updated_at=now_text());append(health_path,proof);atomic(proof_path,proof)
    except BaseException as exc:
        proof.update(status='ERROR',error_type=type(exc).__name__,failure_scope='SHARED_PUBLISHER',failure_stage=locals().get('stage','API_WAIT_OR_INITIALIZATION'),updated_at=now_text());append(health_path,proof);atomic(proof_path,proof)
        raise RuntimeError('TQ_DATA_SIDECAR_STOPPED; SEE_SANITIZED_PROOF') from None
    finally:lease.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--run-id',required=True)
    p.add_argument('--spool',required=True);p.add_argument('--seconds',type=float)
    a=p.parse_args();publish(a.config,a.run_id,a.spool,a.seconds)
