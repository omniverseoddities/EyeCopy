"""Narrow, best-effort Shell refresh after this app publishes a local file.

https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shchangenotify
No directory enumeration, file mutation, UI input, or phone access.
"""
from __future__ import annotations

import ctypes
import re
import sys
from pathlib import PureWindowsPath

CREATE = 0x0002
UPDATEDIR = 0x1000
FLUSHNOWAIT = 0x3000
PATHW = 0x0005
_native = None


class _WindowsShell:
    def __init__(self):
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.ole = ctypes.WinDLL("ole32", use_last_error=True)
        self.shell.SHParseDisplayName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_void_p]
        self.shell.SHParseDisplayName.restype = ctypes.c_int32
        self.shell.SHChangeNotify.argtypes = [ctypes.c_int32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
        self.shell.SHChangeNotify.restype = None
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.ole.CoInitializeEx.restype = ctypes.c_int32
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        self.ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole.CoTaskMemFree.restype = None

    def initialize(self):
        return self.ole.CoInitializeEx(None, 0)  # Transfer worker: MTA.

    def uninitialize(self):
        self.ole.CoUninitialize()

    def parse(self, path):
        pidl = ctypes.c_void_p()
        result = self.shell.SHParseDisplayName(path, None, ctypes.byref(pidl), 0, None)
        if result < 0:
            if pidl.value:
                self.free(pidl.value)
            return None
        return pidl.value

    def free(self, pidl):
        self.ole.CoTaskMemFree(pidl)

    def notify_id(self, event, pidl):
        self.shell.SHChangeNotify(event, FLUSHNOWAIT, pidl, None)

    def notify_path(self, event, path):
        text = ctypes.create_unicode_buffer(path)
        self.shell.SHChangeNotify(event, FLUSHNOWAIT | PATHW, ctypes.cast(text, ctypes.c_void_p), None)


def notify_local_download(path: str, *, bindings=None) -> bool:
    """Submit CREATE + UPDATEDIR for the one committed output and its parent.

    True means notification submission, not that Explorer has repainted.
    PIDLs support long Unicode paths; short PATHW is the only fallback.
    """
    global _native
    if not isinstance(path, str) or "\0" in path or not re.match(r"^[A-Za-z]:[\\/]", path):
        return False
    target = str(PureWindowsPath(path))
    parent = str(PureWindowsPath(target).parent)
    if bindings is None:
        if sys.platform != "win32":
            return False
        try:
            if _native is None:
                _native = _WindowsShell()
            bindings = _native
        except (OSError, AttributeError):
            return False
    initialized = None
    submitted = False
    try:
        initialized = bindings.initialize()
        available = initialized >= 0 or (initialized & 0xffffffff) == 0x80010106
        for event, changed in ((CREATE, target), (UPDATEDIR, parent)):
            pidl = None
            try:
                if available:
                    pidl = bindings.parse(changed)
                if pidl:
                    bindings.notify_id(event, pidl)
                    submitted = True
                elif len(changed.encode("utf-16-le")) // 2 < 260:
                    bindings.notify_path(event, changed)
                    submitted = True
            except Exception:
                # A cosmetic refresh cannot turn a completed copy into failure.
                pass
            finally:
                if pidl:
                    try:
                        bindings.free(pidl)
                    except Exception:
                        pass
    except Exception:
        pass
    finally:
        if initialized is not None and initialized >= 0:
            try:
                bindings.uninitialize()
            except Exception:
                pass
    return submitted
