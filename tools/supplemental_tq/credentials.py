"""Windows-user-bound encrypted TQ authentication; plaintext never written."""
import argparse
import ctypes
from ctypes import wintypes
import getpass
import json
import os
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_=[("size",wintypes.DWORD),("data",ctypes.POINTER(ctypes.c_ubyte))]


def crypt(payload,decrypt=False):
    if os.name!="nt":
        raise RuntimeError("Windows DPAPI required")
    buffer=ctypes.create_string_buffer(payload)
    source=Blob(len(payload),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    target=Blob()
    library=ctypes.WinDLL("crypt32",use_last_error=True)
    if decrypt:
        success=library.CryptUnprotectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target))
    else:
        success=library.CryptProtectData(ctypes.byref(source),"FTQ_TQ_MARKETDATA",None,None,None,1,ctypes.byref(target))
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data,target.size)
    finally:
        ctypes.WinDLL("kernel32").LocalFree(target.data)


def authentication():
    if os.environ.get("TQ_USER") and os.environ.get("TQ_PASSWORD"):
        return os.environ["TQ_USER"],os.environ["TQ_PASSWORD"]
    path=os.environ.get("TQ_AUTH_FILE")
    if not path:
        raise RuntimeError("TQ authentication unavailable")
    decoded=json.loads(crypt(Path(path).read_bytes(),decrypt=True))
    return decoded["user"],decoded["password"]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to overwrite existing authentication file")
    user=getpass.getpass("TQ_USER (hidden): ")
    password=getpass.getpass("TQ_PASSWORD (hidden): ")
    if not user or not password:
        raise SystemExit("Empty authentication rejected")
    encoded=crypt(json.dumps(dict(user=user,password=password)).encode())
    if json.loads(crypt(encoded,decrypt=True))["password"]!=password:
        raise RuntimeError("DPAPI verification failed")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(encoded)
    print("ENCRYPTED_AUTH_SAVED; identity/password not displayed")


if __name__=="__main__":
    main()
