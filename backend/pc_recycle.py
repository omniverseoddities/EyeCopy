"""Recycle/restore exact tracked Windows files, never overwrite or scan the bin.

PostDeleteItem supplies the actual $R destination. Restore uses a verified,
exclusive file handle and FileRenameInfo.ReplaceIfExists=False, then removes
only that receipt's unchanged paired $I metadata. No shell-wide Undo is used.
"""
import contextlib
import ctypes
import hashlib
import os
import re
import uuid
from pathlib import Path


def _ordinary_path(path: Path, directory=False):
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise OSError("Choose an ordinary absolute PC path.")
    for component in [path, *path.parents]:
        value = component.lstat()
        if component.is_symlink() or getattr(value, "st_file_attributes", 0) & 0x400:
            raise OSError("The PC path contains a symbolic link or reparse point.")
    if directory != path.is_dir() or (not directory and not path.is_file()):
        raise OSError("The tracked PC file or folder is unavailable.")


def _api():
    if os.name != "nt":
        raise OSError("Tracked Recycle Bin recovery is available on Windows.")
    from ctypes import wintypes as wt
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    api.CreateFileW.restype = wt.HANDLE
    api.CloseHandle.argtypes = [wt.HANDLE]
    api.CloseHandle.restype = wt.BOOL
    api.GetFileInformationByHandle.argtypes = [wt.HANDLE, ctypes.c_void_p]
    api.GetFileInformationByHandle.restype = wt.BOOL
    api.SetFileInformationByHandle.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    api.SetFileInformationByHandle.restype = wt.BOOL
    api.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    api.ReadFile.restype = wt.BOOL
    api.GetFinalPathNameByHandleW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD]
    api.GetFinalPathNameByHandleW.restype = wt.DWORD
    return api


def _handle_identity(api, handle, directory=False):
    from ctypes import wintypes as wt
    class Info(ctypes.Structure):
        _fields_ = [("attributes", wt.DWORD), ("created", wt.FILETIME), ("accessed", wt.FILETIME),
                    ("modified", wt.FILETIME), ("volume", wt.DWORD), ("sizeHigh", wt.DWORD),
                    ("sizeLow", wt.DWORD), ("links", wt.DWORD), ("indexHigh", wt.DWORD), ("indexLow", wt.DWORD)]
    info = Info()
    if not api.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if bool(info.attributes & 0x10) != directory or info.attributes & 0x400:
        raise OSError("The tracked item is not an ordinary file or folder.")
    identifier = (info.indexHigh << 32) | info.indexLow
    if not identifier or (not directory and info.links != 1):
        raise OSError("This file has no stable unique identity or has multiple links.")
    identity = {"volume": info.volume, "fileId": identifier}
    if not directory:
        identity.update(size=(info.sizeHigh << 32) | info.sizeLow,
                        modified=(info.modified.dwHighDateTime << 32) | info.modified.dwLowDateTime)
    return identity


@contextlib.contextmanager
def _open(path: Path, *, directory=False, exclusive=False, delete=False, read=False):
    _ordinary_path(path, directory)
    api = _api()
    access = 0x80 | (0x10000 if delete else 0) | (0x80000000 if read else 0)
    share = 0 if exclusive else (3 if directory else 7)
    handle = api.CreateFileW(str(path), access, share, None, 3, 0x00200000 | (0x02000000 if directory else 0), None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield api, handle, _handle_identity(api, handle, directory)
    finally:
        api.CloseHandle(handle)


def file_identity(path: Path, directory=False) -> dict:
    with _open(Path(path), directory=directory) as (_, _, identity):
        return identity


def _read_info(api, handle, identity):
    from ctypes import wintypes as wt
    if not 24 <= identity["size"] <= 131072:
        raise OSError("The tracked Recycle Bin metadata is invalid.")
    data = ctypes.create_string_buffer(identity["size"])
    count = wt.DWORD()
    if not api.ReadFile(handle, data, identity["size"], ctypes.byref(count), None) or count.value != identity["size"]:
        raise OSError("The tracked Recycle Bin metadata could not be verified.")
    return hashlib.sha256(data.raw).hexdigest()


def _bin_paths(receipt):
    path = Path(receipt["binPath"])
    if not re.fullmatch(r"[A-Za-z]:\\\$Recycle\.Bin\\S-1-[0-9-]+\\\$R[^\\/:]+", str(path), re.I):
        raise OSError("The recovery receipt does not identify a Windows Recycle Bin file.")
    paired = path.with_name("$I" + path.name[2:])
    if str(paired) != receipt.get("infoPath"):
        raise OSError("The recovery receipt has mismatched Recycle Bin metadata.")
    return path, paired


def validate_receipt(receipt):
    if not isinstance(receipt, dict) or receipt.get("version") != 1 or not isinstance(receipt.get("id"), str):
        raise OSError("The recovery receipt is invalid.")
    original = Path(receipt["originalPath"])
    if not original.is_absolute() or original.parent == original or not original.name:
        raise OSError("The recovery receipt has an invalid original path.")
    _bin_paths(receipt)
    for field in ("originalIdentity", "parentIdentity", "binIdentity"):
        if not isinstance(receipt.get(field), dict) or not receipt[field].get("fileId"):
            raise OSError("The recovery receipt is missing a verified file identity.")
    if receipt["originalIdentity"] != receipt["binIdentity"]:
        raise OSError("The recovery receipt has mismatched original and recycled identities.")
    if receipt.get("capturePending"):
        return
    if not isinstance(receipt.get("infoIdentity"), dict) or not receipt["infoIdentity"].get("fileId"):
        raise OSError("The recovery receipt is missing verified metadata identity.")
    if not re.fullmatch(r"[0-9a-f]{64}", receipt.get("infoDigest", "")):
        raise OSError("The recovery receipt is missing its metadata digest.")


def _base_receipt(path, original, parent, bin_path):
    bin_path = Path(bin_path)
    receipt = {"version": 1, "id": uuid.uuid4().hex, "originalPath": str(path),
               "originalIdentity": original, "parentIdentity": parent, "binPath": str(bin_path),
               "infoPath": str(bin_path.with_name("$I" + bin_path.name[2:]))}
    _bin_paths(receipt)
    receipt.update(binIdentity=original, recycled=True)
    return receipt


def _capture_receipt(path, original, parent, bin_path):
    receipt = _base_receipt(path, original, parent, bin_path)
    bin_path = Path(receipt["binPath"])
    receipt["binIdentity"] = file_identity(bin_path)
    if receipt["binIdentity"] != original:
        raise OSError("Windows recycled the file, but its recovery identity changed.")
    with _open(Path(receipt["infoPath"]), read=True) as (api, handle, identity):
        receipt["infoIdentity"] = identity
        receipt["infoDigest"] = _read_info(api, handle, identity)
    validate_receipt(receipt)
    return receipt


def repair_recycle_receipt(receipt):
    """Repair only a previously captured exact destination, never find another."""
    validate_receipt(receipt)
    if not receipt.get("capturePending"):
        return receipt
    repaired = _capture_receipt(Path(receipt["originalPath"]), receipt["originalIdentity"],
                                receipt["parentIdentity"], receipt["binPath"])
    repaired["id"] = receipt["id"]
    if receipt.get("originalRecord") is not None:
        repaired["originalRecord"] = receipt["originalRecord"]
    return repaired


def recycle_file(path: Path, validate) -> dict:
    import pythoncom
    from win32com.server.exception import COMException
    from win32com.server.policy import DesignatedWrapPolicy
    from win32com.shell import shell, shellcon

    path = Path(path).absolute()
    original = file_identity(path)
    parent = file_identity(path.parent, True)
    destination = {}

    class Sink(DesignatedWrapPolicy):
        _com_interfaces_ = [shell.IID_IFileOperationProgressSink]
        _public_methods_ = ["StartOperations", "FinishOperations", "PreRenameItem", "PostRenameItem",
                           "PreMoveItem", "PostMoveItem", "PreCopyItem", "PostCopyItem", "PreDeleteItem",
                           "PostDeleteItem", "PreNewItem", "PostNewItem", "UpdateProgress", "ResetTimer",
                           "PauseTimer", "ResumeTimer"]

        def __init__(self):
            self.recycled = False
            self._wrap_(self)

        def PreDeleteItem(self, flags, item):
            # Match Electron's Windows trash implementation: abort if Windows
            # would bypass recycling, including unsupported/network volumes.
            if not flags & shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE or not validate() or file_identity(path) != original:
                raise COMException(desc="File cannot be safely recycled.", hresult=-2147467259)

        def PostDeleteItem(self, flags, item, result, newly_created):
            # Windows reports COPYENGINE_S_* (nonzero successful HRESULTs) for
            # recycling. HRESULT success depends on its high bit, not equality
            # with S_OK; still require the actual Recycle Bin destination.
            self.recycled = (result & 0x80000000) == 0 and newly_created is not None
            if self.recycled:
                for kind in (shellcon.SIGDN_FILESYSPATH, shellcon.SIGDN_DESKTOPABSOLUTEPARSING):
                    try:
                        value = newly_created.GetDisplayName(kind)
                        if isinstance(value, str) and re.fullmatch(r"[A-Za-z]:\\\$Recycle\.Bin\\S-1-[0-9-]+\\\$R[^\\/:]+", value, re.I):
                            destination["binPath"] = value
                            break
                    except Exception:
                        pass

    def ignore(self, *args):
        return None
    for method in Sink._public_methods_:
        if not hasattr(Sink, method):
            setattr(Sink, method, ignore)

    pythoncom.CoInitialize()
    try:
        sink = Sink()
        wrapped = pythoncom.WrapObject(sink, shell.IID_IFileOperationProgressSink)
        operation = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None,
                                               pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IFileOperation)
        # RECYCLEONDELETE plus a sink veto: never fall back to permanent remove.
        operation.SetOperationFlags(0x80000 | shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION
                                    | shellcon.FOF_SILENT | shellcon.FOF_NOERRORUI | 0x100000)
        item = shell.SHCreateItemFromParsingName(str(path), None, shell.IID_IShellItem)
        operation.DeleteItem(item, wrapped)
        operation.PerformOperations()
        if operation.GetAnyOperationsAborted() or not sink.recycled or path.exists():
            raise OSError("Windows did not confirm a Recycle Bin destination.")
        if "binPath" not in destination:
            # The Shell confirmed recycling but did not expose a durable exact
            # destination. Report physical truth; do not manufacture an Undo
            # target or search the bin to guess which item was created.
            return {"version": 1, "id": uuid.uuid4().hex, "originalPath": str(path),
                    "recycled": True, "undoUnavailable": True,
                    "receiptWarning": "Windows recycled this file but did not expose its exact recovery entry. Restore it using the Recycle Bin."}
        try:
            return _capture_receipt(path, original, parent, destination["binPath"])
        except OSError:
            # PostDeleteItem and the absent source already prove physical
            # recycling. Keep that exact destination and report repair pending
            # rather than misreporting an unchanged source. Undo will require
            # full identity/metadata repair before opening a mutation handle.
            receipt = _base_receipt(path, original, parent, destination["binPath"])
            receipt["capturePending"] = True
            return receipt
    finally:
        pythoncom.CoUninitialize()


def restore_recycled_file(receipt: dict, validate=lambda: True) -> dict:
    """Restore only the exact receipt, using an atomic no-replace handle rename."""
    validate_receipt(receipt)
    if receipt.get("capturePending"):
        raise OSError("Recovery metadata is still being prepared. Retry Undo shortly.")
    bin_path, info_path = _bin_paths(receipt)
    original = Path(receipt["originalPath"])
    if os.path.lexists(original):
        raise FileExistsError("A file already exists at the original location. It was kept; choose another location for it before retrying Undo.")
    from ctypes import wintypes as wt
    class Rename(ctypes.Structure):
        _fields_ = [("replace", wt.DWORD), ("root", wt.HANDLE), ("length", wt.DWORD), ("name", wt.WCHAR * 1)]
    with _open(original.parent, directory=True) as (_, parent_handle, parent), \
            _open(bin_path, exclusive=True, delete=True) as (api, source_handle, source), \
            _open(info_path, exclusive=True, delete=True, read=True) as (_, info_handle, info):
        if parent != receipt["parentIdentity"] or source != receipt["binIdentity"] or info != receipt["infoIdentity"] or _read_info(api, info_handle, info) != receipt["infoDigest"] or not validate():
            raise OSError("The tracked file, Recycle Bin entry, or original folder changed. Undo kept the files untouched.")
        # Keep the verified parent open without delete sharing. The Win32
        # FileRenameInfo path is absolute (the tested API rejects a relative
        # RootDirectory variant). ReplaceIfExists stays false even if a new
        # destination appears after the earlier existence check.
        encoded = str(original).encode("utf-16-le")
        buffer = ctypes.create_string_buffer(max(ctypes.sizeof(Rename), Rename.name.offset + len(encoded) + 2))
        rename = Rename.from_buffer(buffer); rename.replace = 0; rename.root = None; rename.length = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + Rename.name.offset, encoded, len(encoded))
        if not api.SetFileInformationByHandle(source_handle, 3, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
        actual = ctypes.create_unicode_buffer(32768)
        length = api.GetFinalPathNameByHandleW(source_handle, actual, len(actual), 0)
        value = actual.value
        if value.startswith("\\\\?\\"):
            value = value[4:]
        if not length or length >= len(actual) or os.path.normcase(value) != os.path.normcase(str(original)):
            # The exact open source handle still identifies our file. Roll it
            # back without replacing another bin item before reporting failure.
            rollback = str(bin_path).encode("utf-16-le")
            back = ctypes.create_string_buffer(max(ctypes.sizeof(Rename), Rename.name.offset + len(rollback) + 2))
            fields = Rename.from_buffer(back); fields.replace = 0; fields.root = None; fields.length = len(rollback)
            ctypes.memmove(ctypes.addressof(back) + Rename.name.offset, rollback, len(rollback))
            if not api.SetFileInformationByHandle(source_handle, 3, back, len(back)):
                raise OSError("Windows moved the tracked file to an unexpected location and could not roll it back. Keep this recovery receipt and check the PC folder.")
            raise OSError("Windows returned an unexpected restore destination. The tracked bin file was kept; retry Undo.")
        restored = _handle_identity(api, source_handle)
        delete_metadata = ctypes.c_ubyte(1)
        if not api.SetFileInformationByHandle(info_handle, 4, ctypes.byref(delete_metadata), ctypes.sizeof(delete_metadata)):
            # The actual file has already been restored; never label it as a
            # failure or recycle it again merely because metadata cleanup waits.
            return {"restoredIdentity": restored, "infoCleanupPending": True}
    try:
        from win32com.shell import shell, shellcon
        for event, target in [(shellcon.SHCNE_DELETE, bin_path), (shellcon.SHCNE_DELETE, info_path), (shellcon.SHCNE_CREATE, original)]:
            shell.SHChangeNotify(event, shellcon.SHCNF_PATHW, str(target), None)
    except Exception:
        pass
    return {"restoredIdentity": restored, "infoCleanupPending": False}


def cleanup_recycle_metadata(receipt: dict) -> bool:
    """Finish only the paired metadata of an already restored tracked file."""
    validate_receipt(receipt)
    bin_path, info_path = _bin_paths(receipt)
    if os.path.lexists(bin_path):
        raise OSError("The recycled file is still present; metadata was kept.")
    if not os.path.lexists(info_path):
        return True
    with _open(info_path, exclusive=True, delete=True, read=True) as (api, handle, identity):
        if identity != receipt["infoIdentity"] or _read_info(api, handle, identity) != receipt["infoDigest"]:
            raise OSError("The tracked Recycle Bin metadata changed; it was kept.")
        remove = ctypes.c_ubyte(1)
        if not api.SetFileInformationByHandle(handle, 4, ctypes.byref(remove), ctypes.sizeof(remove)):
            raise ctypes.WinError(ctypes.get_last_error())
    return True
