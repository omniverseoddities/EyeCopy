"""Bounded, confirmed shared-app folder deletion without recursive AFC removal."""
from __future__ import annotations

from datetime import datetime
import posixpath


class FolderChangedError(ValueError):
    pass


def _path(value):
    if (not isinstance(value, str) or "\\" in value or "\x00" in value
            or any(part in {".", ".."} for part in value.split("/"))):
        raise ValueError("The selected Device folder path is invalid.")
    value = posixpath.normpath(value)
    if not value.startswith("/Documents/"):
        raise ValueError("The app's Documents root cannot be deleted.")
    return value


def _stamp(value):
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def _signature(value):
    return {"kind": value.get("st_ifmt"), "size": int(value.get("st_size", 0)),
            "modified": _stamp(value.get("st_mtime")), "created": _stamp(value.get("st_birthtime"))}


def _directory_chain(afc, path):
    current = "/Documents"
    if afc.stat(current).get("st_ifmt") != "S_IFDIR":
        raise ValueError("The app documents folder is unavailable.")
    for name in path[len(current):].strip("/").split("/") if path != current else []:
        current = posixpath.join(current, name)
        if afc.stat(current).get("st_ifmt") != "S_IFDIR":
            raise ValueError("A folder became unavailable or is a symbolic link. Refresh and review it again.")


def _children(afc, path):
    names = []
    for name in afc.listdir(path):
        if name in {".", ".."}:
            continue
        if (not isinstance(name, str) or not name or any(c in name for c in ("/", "\\", "\x00"))):
            raise ValueError("The folder contains an invalid file name and was kept.")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("The folder listing changed. Refresh and review it again.")
    return sorted(names)


def snapshot(afc, path, *, max_entries=5000, max_depth=64):
    """Record exact approved names/types/metadata; reject links and special files."""
    root = _path(path)
    _directory_chain(afc, posixpath.dirname(root))
    entries = []
    pending = [(root, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > max_depth or len(entries) >= max_entries:
            raise ValueError("This folder is too large to delete in one action. Delete smaller subfolders first.")
        details = _signature(afc.stat(current))
        if details["kind"] not in {"S_IFDIR", "S_IFREG"}:
            raise ValueError("Folders containing symbolic links or special files cannot be deleted safely.")
        entry = {"path": current, "signature": details}
        if details["kind"] == "S_IFDIR":
            names = _children(afc, current)
            entry["children"] = names
            pending.extend((posixpath.join(current, name), depth + 1) for name in reversed(names))
        entries.append(entry)
    return {"root": root, "entries": entries,
            "fileCount": sum(entry["signature"]["kind"] == "S_IFREG" for entry in entries),
            "folderCount": sum(entry["signature"]["kind"] == "S_IFDIR" for entry in entries),
            "bytes": sum(entry["signature"]["size"] for entry in entries if entry["signature"]["kind"] == "S_IFREG")}


def validate(afc, approved):
    if snapshot(afc, approved["root"]) != approved:
        raise FolderChangedError("The Device folder changed after the warning. Nothing was deleted; review it again.")


def remove(afc, approved):
    """Delete only recorded files, then empty directories; keep new/changed data."""
    validate(afc, approved)
    removed = []
    try:
        entries = approved["entries"]
        files = [entry for entry in entries if entry["signature"]["kind"] == "S_IFREG"]
        folders = sorted((entry for entry in entries if entry["signature"]["kind"] == "S_IFDIR"),
                         key=lambda entry: entry["path"].count("/"), reverse=True)
        for entry in files:
            path = _path(entry["path"])
            _directory_chain(afc, posixpath.dirname(path))
            if _signature(afc.stat(path)) != entry["signature"]:
                raise FolderChangedError("A file changed during deletion and was kept. Refresh to review what remains.")
            afc.rm_single(path)
            if afc.exists(path):
                raise ValueError("The Device did not confirm removal. Refresh to check what remains.")
            removed.append(path)
        for entry in folders:
            path = _path(entry["path"])
            _directory_chain(afc, path)
            # Directory timestamps naturally change after removing children.
            # Require empty contents instead of deleting any newly added data.
            if _children(afc, path):
                raise FolderChangedError("New files appeared during deletion and were kept. Refresh to review what remains.")
            afc.rm_single(path)
            if afc.exists(path):
                raise ValueError("The Device did not confirm folder removal. Refresh to check what remains.")
            removed.append(path)
        return {"deleted": True, "removedPaths": removed}
    except Exception as error:
        return {"deleted": False, "removedPaths": removed, "error": str(error)}
