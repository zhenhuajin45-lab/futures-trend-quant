"""Bounded existing-environment entitlement check; never record account/token."""
from pathlib import Path
from datetime import datetime,timezone
from io import StringIO
import json,os,sys
import argparse,ctypes
from ctypes import wintypes
import requests,pandas as pd

ROOT=Path(__file__).resolve().parents[2]


def local_dpapi_credentials(path):
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    payload=path.read_bytes();buffer=ctypes.create_string_buffer(payload)
    source=Blob(len(payload),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)));target=Blob()
    library=ctypes.WinDLL('crypt32',use_last_error=True)
    if not library.CryptUnprotectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:return json.loads(ctypes.string_at(target.data,target.size))
    finally:ctypes.WinDLL('kernel32').LocalFree(target.data)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--dpapi-auth-file',type=Path);args=parser.parse_args()
    result=dict(observed_at=datetime.now(timezone.utc).isoformat(),mode='EXISTING_LOCAL_ENVIRONMENT_ONLY',
        account_identifiers_recorded=False,source='EDB',status='NO_CONFIGURED_AUTH_IN_CURRENT_ENVIRONMENT',
        bigquant_environment_present=bool(os.environ.get('BIGQUANT_TOKEN') or os.environ.get('BIGQUANT_API_KEY')))
    token=os.environ.get('EDB_TOKEN')
    try:
        authentication=None
        if not token and os.environ.get('TQ_USER') and os.environ.get('TQ_PASSWORD'):
            authentication=dict(user=os.environ['TQ_USER'],password=os.environ['TQ_PASSWORD'])
        elif not token and args.dpapi_auth_file:
            authentication=local_dpapi_credentials(args.dpapi_auth_file)
            result.update(mode='EXISTING_LOCAL_ENVIRONMENT_AND_DPAPI',existing_encrypted_auth_found=True,
                          credential_source='EXISTING_WINDOWS_DPAPI_FILE')
        if not token and authentication:
            response=requests.post('https://edb.shinnytech.com/token',json=dict(username=authentication['user'],
                password=authentication['password']),timeout=(5,8))
            result['token_http_status']=response.status_code
            if response.ok:token=response.json().get('token')
            else:result['status']='EXISTING_DPAPI_AUTH_NOT_ACCEPTED' if args.dpapi_auth_file else 'EXISTING_AUTH_NOT_ACCEPTED'
        if token:
            response=requests.get('https://edb.shinnytech.com/md/kline',params=dict(symbol='DCE.m2605',period=60,
                start_time='2025-09-15 09:00:00',end_time='2025-09-15 09:05:00',col='volume'),
                headers={'Authorization':'Bearer '+token},timeout=(5,8))
            result['history_http_status']=response.status_code
            result['status']='EARLY_CLOCK_ROWS_RECEIVED' if response.ok and response.text.startswith('datetime_nano,') else 'EARLY_HISTORY_NOT_GRANTED'
            if result['status']=='EARLY_CLOCK_ROWS_RECEIVED':result['rows']=len(pd.read_csv(StringIO(response.text)))
    except requests.RequestException as exc:result.update(status='REQUEST_FAILED',error_type=type(exc).__name__)
    except (ValueError,KeyError,OSError) as exc:result.update(status='AUTH_RESPONSE_NOT_USABLE',error_type=type(exc).__name__)
    output=ROOT/'data/cache/v71_source_inventory/entitlement_check.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    print(json.dumps(result))


if __name__=='__main__':main()
