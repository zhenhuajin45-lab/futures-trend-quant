"""One native SIM writer per explicit account; read collectors never own this lease."""
from contextlib import contextmanager
import ctypes,hashlib


@contextmanager
def writer_lease(account):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.restype=ctypes.c_void_p
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    ctypes.set_last_error(0)
    handle=kernel.CreateMutexW(None,False,'Local\\FTQ_NATIVE_SIM_WRITER_'+hashlib.sha256(account.encode()).hexdigest()[:24])
    exists=ctypes.get_last_error()==183
    if not handle or exists:
        if handle:kernel.CloseHandle(handle)
        raise RuntimeError('NATIVE_SIM_WRITER_ALREADY_OWNED')
    try:yield
    finally:kernel.CloseHandle(handle)
