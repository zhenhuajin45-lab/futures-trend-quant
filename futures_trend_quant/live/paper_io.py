"""Bounded, replace-compatible JSON reads. No cached/stale proof fallback."""
from pathlib import Path
import ctypes
import errno
import json
import os
import time


def native_path(path):
    value=str(Path(path).resolve())
    if os.name!='nt' or value.startswith('\\\\?\\'):return value
    return '\\\\?\\UNC\\'+value[2:] if value.startswith('\\\\') else '\\\\?\\'+value


class JsonReadError(OSError):
    def __init__(self, path, stage, original, attempts):
        self.details = dict(code='JSON_READ_FAILED', file=Path(path).name,
                            stage=stage, category=type(original).__name__,
                            errno=getattr(original, 'errno', None),
                            winerror=getattr(original, 'winerror', None), attempts=attempts)
        super().__init__(getattr(original, 'errno', None),
                         'JSON_READ_FAILED:'+Path(path).name+':'+stage)


def shared_open(path):
    """Windows readers allow the publisher's atomic replace, including DELETE."""
    if os.name != 'nt':
        return open(path, 'rb')
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    handle = create(native_path(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(fd, 'rb')


def read_json(path):
    """Three attempts, at most 15ms retry sleep; parse failures never use old data."""
    for attempt in range(3):
        stage = 'OPEN'
        try:
            with shared_open(path) as stream:
                stage = 'READ'
                raw = stream.read()
            stage = 'DECODE_JSON'
            return json.loads(raw.decode('utf-8-sig'))
        except (OSError, UnicodeError, ValueError) as exc:
            transient = isinstance(exc, PermissionError) and (
                getattr(exc, 'winerror', None) in (5, 32, 33)
                or getattr(exc, 'errno', None) == errno.EACCES)
            if transient and attempt < 2:
                time.sleep(.005 * (attempt+1))
                continue
            raise JsonReadError(path, stage, exc, attempt+1) from exc


def replace_file(source,target):
    """Atomic Windows replacement, including DELETE-sharing reader handles."""
    if os.name!='nt' or not Path(target).exists():
        os.replace(source,target)
        return
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    replace=kernel.ReplaceFileW
    replace.argtypes=(wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD,wintypes.LPVOID,wintypes.LPVOID)
    replace.restype=wintypes.BOOL
    if not replace(native_path(target),native_path(source),None,0,None,None):
        raise ctypes.WinError(ctypes.get_last_error())


class JsonlTail:
    """Incremental immutable JSONL reader; a partial trailing line stays unread.

    Truncation resets the cursor. Consumers dedupe by stable event identity.
    Closed log rows are never silently skipped on decoding failures.
    """
    def __init__(self):
        self.offset = 0
        self.identity = None
        self.anchor = b''

    def read(self, path):
        path = Path(path)
        if not path.exists():
            return []
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity != self.identity or stat.st_size < self.offset:
            self.offset = 0
            self.identity = identity
        result = []
        with shared_open(path) as stream:
            if self.offset:
                stream.seek(max(0,self.offset-len(self.anchor)))
                if stream.read(len(self.anchor))!=self.anchor:self.offset=0
            stream.seek(self.offset)
            while True:
                start = stream.tell()
                line = stream.readline()
                if not line or not line.endswith(b'\n'):
                    break
                value = json.loads(line.decode('utf-8-sig'))
                result.append(value)
                self.offset = stream.tell()
                assert self.offset > start
            stream.seek(max(0,self.offset-64))
            self.anchor=stream.read(self.offset-max(0,self.offset-64))
        return result
