"""Hash, identity, signoff and protected-output checks; never infer signatures."""
from pathlib import Path
import hashlib,json
from datetime import datetime
import platform,importlib.metadata
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
EXPECTED=dict(zip='9f1560977bb94432b440bbd96fbdb77c872e800b2c5c8a7363cb93961e005bc6',
    diagnose='93602720fc1a17e13c7f47b1c9a2a701e0bc351412837e58e4e946ac16c278d5',
    prepared='3709ad6bfc354efb1474ae59f71c0bdb40f0d54a210ce6c065ff902651a972ee',
    input_lock='6dfd477e14f03aa0dcd6d0b0dd69fd1f1b8683ea16fadfd44a1ab86e6ac0c97d')
REQUIRED_FILES={f'tools/v11_diagnostic/{name}' for name in ('__init__.py','contracts.py','statistics.py','summarize.py','workflow.py','prereg.json','environment.json','taskbook.md','reference_tool/diagnose.py','reference_tool/test_diagnose.py')}|{'tests/test_v11_diagnostic.py'}


def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))


def sha(p,text=False):
    if text:return hashlib.sha256(Path(p).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def write(p,obj):
    Path(p).write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')


def object_sha(obj):return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def queue_identity(f):
    cols=['trade_id','csymbol','symbol','entry_day']
    return object_sha(f[cols].sort_values('trade_id').to_dict('records'))


def fresh_output(path,real):
    p=Path(path).resolve()
    if p.exists():raise ValueError('output directory already exists')
    if real and not p.is_relative_to((ROOT/'data/cache/v11_restricted').resolve()):
        raise ValueError('formal detail outputs must stay in local restricted cache')
    if any(x in p.parts for x in ('.git','reports','docs','tools','config','tests')):raise ValueError('protected output location')
    return p


def validate_freeze(prereg,receipts):
    reg=read(prereg);folder=Path(prereg).parent;manifest_path=folder/'freeze_manifest.json'
    if reg.get('status')!='FROZEN_FOR_ONE_OLD_WINDOW_DIAGNOSTIC':raise ValueError('draft registration not accepted')
    m=read(manifest_path)
    if m.get('hash_convention')!='SHA256_LF_NORMALIZED_PROJECT_TEXT_RAW_EXTERNAL_BYTES':raise ValueError('manifest convention mismatch')
    if set(m['files'])!=REQUIRED_FILES:raise ValueError('incomplete frozen code inventory')
    for rel,h in m['files'].items():
        p=(ROOT/rel).resolve()
        if not p.is_relative_to(ROOT) or sha(p,True)!=h:raise ValueError('code or registration digest mismatch: '+rel)
    if m['originals']!=EXPECTED:raise ValueError('original expected hashes changed')
    environment='tools/v11_diagnostic/environment.json'
    if environment in m['files']:
        env=read(ROOT/environment)
        if env['python']!=platform.python_version() or any(env[k]!=importlib.metadata.version(k) for k in ('pandas','numpy')):
            raise ValueError('frozen environment version mismatch; no automatic upgrade')
    if str(Path(prereg).resolve().relative_to(ROOT)).replace('\\','/') not in m['files']:raise ValueError('registration not in manifest')
    manifest_sha=sha(manifest_path,True);roles=[]
    for name,role in [('execution.json','EXECUTOR'),('review.json','REVIEWER')]:
        p=Path(receipts)/name
        if not p.is_file():raise ValueError('AWAITING_SIGNOFF: '+name)
        r=read(p)
        if r.get('state')!='SIGNED' or r.get('role')!=role or not r.get('identity'):raise ValueError('AWAITING_SIGNOFF: incomplete receipt')
        if r.get('manifest_sha256')!=manifest_sha:raise ValueError('receipt digest mismatch')
        t=datetime.fromisoformat(r['recorded_at_beijing'])
        if t.tzinfo is None or t.utcoffset().total_seconds()!=28800:raise ValueError('receipt Beijing clock missing')
        roles.append(r)
    same=roles[0]['identity']==roles[1]['identity']
    if same:
        if reg.get('review_mode')!='SINGLE_AGENT_USER_OVERRIDE' or any(r.get('independent_person') is not False for r in roles):
            raise ValueError('same person cannot masquerade as independent dual signoff')
        if any(r.get('user_authority')!='USER_ASSIGNED_BOTH_ROLES_TO_CODEX' for r in roles):raise ValueError('user role override not recorded')
    elif reg.get('review_mode')!='INDEPENDENT_TWO_PERSON':raise ValueError('review identity contract changed')
    archive_path=Path(receipts)/'timestamp_archive.json'
    archive=read(archive_path)
    if archive.get('manifest_sha256')!=manifest_sha or archive.get('kind')!='GITHUB_FREEZE_HTTP_DATE':raise ValueError('independent timestamp archive absent')
    if not archive.get('server_date_utc') or not archive.get('freeze_commit') or not archive.get('remote_manifest_blob'):raise ValueError('timestamp archive incomplete')
    return reg,dict(manifest_sha256=manifest_sha,review_mode=reg['review_mode'],
        independent_two_person_review=not same,signoff_identities=[x['identity'] for x in roles],
        timestamp_archive_sha256=sha(archive_path),freeze_commit=archive['freeze_commit'])


def verify_original_paths(provenance):
    paths=provenance.get('original_paths',{})
    if set(paths)!={'prepared','input_lock'}:raise ValueError('original byte paths absent')
    if Path(paths['prepared']).parent!=Path(paths['input_lock']).parent:raise ValueError('input_lock must accompany prepared')
    for name,path in paths.items():
        if not Path(path).is_file() or sha(path)!=EXPECTED[name]:raise ValueError('original bytes hash mismatch: '+name)


def validate_audit(input_dir,reg,binding):
    folder=Path(input_dir);provenance=read(folder/'provenance.json')
    if provenance.get('data_kind')!='REAL_OLD_WINDOW':raise ValueError('synthetic/draft input cannot enter formal outputs')
    if provenance.get('manifest_sha256')!=binding['manifest_sha256']:raise ValueError('input generated by different frozen code')
    if provenance.get('originals')!=EXPECTED:raise ValueError('wrong original input hashes')
    verify_original_paths(provenance)
    if provenance.get('audit_sha256')!=sha(folder/'audit.csv'):raise ValueError('audit digest mismatch')
    f=pd.read_csv(folder/'audit.csv');summary=read(folder/'summary.json')
    if (summary.get('closed'),summary.get('open_excluded'),summary.get('known_time'),summary.get('clock_hashes_matched'))!=(631,5,629,12):
        raise ValueError('old diagnostic cohort/calendar denominators mismatch')
    source=ROOT/'reports/v10_same_discipline/GROSS_ORDINARY_SHORT_trades.csv'
    # V10 LF hash, not merely a matching filename or a caller-provided count.
    if sha(source,True)!=reg['cohort']['ledger_lf_sha256']:raise ValueError('frozen ledger changed')
    original=pd.read_csv(source)
    if f.trade_id.duplicated().any() or queue_identity(f)!=reg['cohort']['queue_identity_sha256'] or queue_identity(f)!=queue_identity(original):
        raise ValueError('input not original queue')
    a=f.set_index('trade_id').sort_index();b=original.set_index('trade_id').sort_index()
    import numpy as np
    if not np.allclose(a.gross_r,b.gross_r,rtol=0,atol=1e-12):raise ValueError('audit rewrites frozen gross R')
    if f.remaining_minutes.notna().sum()!=629:raise ValueError('audit time coverage mismatch')
    return f,dict(audit_sha256=sha(folder/'audit.csv'),provenance_sha256=sha(folder/'provenance.json'),
        original_queue_sha256=queue_identity(f),calendar_fingerprints_matched=12)
