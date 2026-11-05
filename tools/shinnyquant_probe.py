"""Three fixed, token-free source smoke checks; no price/outcome calculation."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import importlib.metadata as metadata
import json
import pandas as pd
from requests.auth import AuthBase

ROOT=Path(__file__).resolve().parents[1]
CASES=[
    dict(name='RECENT_M1_CLOCK_ONLY',symbol='DCE.m2701',period=60,start_time='2026-11-03 09:00:00',end_time='2026-11-03 09:05:00'),
    dict(name='EARLY_M1_CLOCK_ONLY',symbol='DCE.m2405',period=60,start_time='2024-01-15 09:00:00',end_time='2024-01-15 09:05:00'),
    dict(name='EARLY_DAILY_VOLUME_ONLY',symbol='DCE.m2405',period=86400,start_time='2024-01-15 00:00:00',end_time='2024-01-15 23:59:59'),
]


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')


def run(out):
    out=out.resolve()
    if not out.is_relative_to((ROOT/'data/cache').resolve()):
        raise ValueError('raw responses must stay in ignored local data/cache')
    if out.exists():raise ValueError('fresh output required; do not retry this attempt')
    policy_path=ROOT/'tools/mechanism_preflight/protected.json'
    policy=json.loads(policy_path.read_text(encoding='utf-8-sig'))
    for case in CASES:
        a=pd.Timestamp(case['start_time'],tz='Asia/Shanghai');b=pd.Timestamp(case['end_time'],tz='Asia/Shanghai')
        for interval in policy['intervals']:
            if b>=pd.Timestamp(interval['start']) and (interval['end'] is None or a<=pd.Timestamp(interval['end'])):
                raise ValueError('protected range: rejected before SDK import or request')
    out.mkdir(parents=True)
    plan=dict(cases=CASES,columns=['volume'],created_at=datetime.now(timezone.utc).isoformat(),
              use='SOURCE_CLOCK_SMOKE_ONLY_NOT_INDEPENDENT_SAMPLE_ADMISSION',
              protected_sha256=hashlib.sha256(policy_path.read_bytes()).hexdigest())
    write(out/'plan.json',plan)
    from shinny_quant import ShinnyQuantClient,ApiError
    import shinny_quant
    journal=[]
    result=dict(distribution_version=metadata.version('shinny-quant'),module_version=shinny_quant.__version__,
        observed_at=datetime.now(timezone.utc).isoformat(),plan_sha256=hashlib.sha256((out/'plan.json').read_bytes()).hexdigest(),
        authenticated=False,credential_access=False,login_attempts=0,orders=False,subscriptions=False,
        protected_prices_read=False,ohlc_requested=False,future_response_computed=False,cases=[])
    with ShinnyQuantClient(timeout=15,debug=False) as client:
        assert not client.is_authenticated and client.token is None
        class NoAuthentication(AuthBase):
            def __call__(self,request):return request
        client._session.auth=NoAuthentication()  # Prevent implicit .netrc authentication.
        def capture(response,*args,**kwargs):
            if response.request.method!='GET' or not response.url.startswith('https://edb.shinnytech.com/md/kline?'):
                raise ValueError('unexpected endpoint')
            if response.request.headers.get('Authorization'):
                raise ValueError('unexpected authorization')
            body=response.content
            path=out/f'http_{len(journal)+1}.raw'
            path.write_bytes(body)
            journal.append(dict(http_status=response.status_code,bytes=len(body),sha256=hashlib.sha256(body).hexdigest()))
        client._session.hooks['response']=[capture]
        for case in CASES:
            row=dict(case)
            try:
                frame=client.get_kline(**{k:v for k,v in case.items() if k!='name'},columns=['volume'])
                row.update(status='DATAFRAME_RECEIVED',rows=len(frame),return_type=type(frame).__name__,columns=list(frame.columns),
                    index_timezone=str(frame.index.tz) if isinstance(frame.index,pd.DatetimeIndex) else None,
                    first_bob=None if frame.empty else str(frame.index.min()),last_bob=None if frame.empty else str(frame.index.max()))
                frame.to_parquet(out/(case['name']+'.parquet'))
                if case['period']==60 and not frame.empty:
                    row.update(first_eob=str(frame.index.min()+pd.Timedelta(minutes=1)),last_eob=str(frame.index.max()+pd.Timedelta(minutes=1)),
                        duplicates=int(frame.index.duplicated().sum()),minute_label='SDK_DATETIME_IS_BOB; EOB=BOB+60S')
            except ApiError as exc:
                row.update(status='API_ERROR',provider_status=exc.status_code,error_type=type(exc).__name__)
            except Exception as exc:
                row.update(status='REQUEST_OR_PARSE_FAILED',error_type=type(exc).__name__)
            result['cases'].append(row)
    result['http_journal']=journal
    result['http_requests']=len(journal)
    result['full_day_or_full_range_qualified']=False
    write(out/'result.json',result)
    write(out/'manifest.json',dict(files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()}))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True)
    print(json.dumps(run(parser.parse_args().out),ensure_ascii=False))
