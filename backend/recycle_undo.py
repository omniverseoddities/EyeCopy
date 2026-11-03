"""Durable, token-ordered Undo/Redo for EyeCopy's exact recycled PC files.

This history never scans the Recycle Bin and never invokes Windows' global
Undo. Per-file phases make partial batch failures retryable without touching
already restored/recycled files. A separate journal survives a blocked main
history replacement, and successful physical results stay in memory if saving
is temporarily unavailable.
"""
import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path

from backend.pc_recycle import (cleanup_recycle_metadata, file_identity,
                               recycle_file, restore_recycled_file, validate_receipt,
                               repair_recycle_receipt)


class RecycleUndoHistory:
    def __init__(self, data_dir, *, recycler=recycle_file, restorer=restore_recycled_file,
                 identity=file_identity, cleanup=cleanup_recycle_metadata, repair=repair_recycle_receipt):
        self.directory = Path(data_dir).absolute()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "recycle-undo.json"
        self.journal = self.directory / "recycle-undo-journal.json"
        self.lock = threading.RLock()
        self.recycler, self.restorer, self.identity, self.cleanup = recycler, restorer, identity, cleanup
        self.repair = repair
        self.entries, self.cursor, self.revision = [], 0, 0
        self.dirty = False
        self.load_warning = False
        self._load()

    def _load(self):
        candidates = []
        for path in (self.path, self.journal):
            if not path.exists():
                continue
            try:
                if path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError("Invalid history file.")
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("version") != 1 or not isinstance(value.get("entries"), list) or not 0 <= value["cursor"] <= len(value["entries"]) or len(value["entries"]) > 40:
                    raise ValueError("Invalid history shape.")
                seen = set()
                for entry in value["entries"]:
                    if not isinstance(entry.get("token"), str) or entry["token"] in seen or not 1 <= len(entry.get("files", [])) <= 20000:
                        raise ValueError("Invalid history action.")
                    seen.add(entry["token"])
                    for row in entry["files"]:
                        validate_receipt(row["receipt"])
                        if row.get("phase") not in {"recycled", "restored"}:
                            raise ValueError("Invalid recovery phase.")
                candidates.append((int(value.get("revision", 0)), path, value))
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
                self.load_warning = True
        if candidates:
            _, chosen, value = max(candidates, key=lambda item: (item[0], item[1] == self.journal))
            self.entries, self.cursor, self.revision = value["entries"], value["cursor"], value.get("revision", 0)
            self.dirty = chosen == self.journal
        # A crash after an atomic restore can be recognized from this exact
        # receipt's file identity at the original path. No directory is searched.
        for entry in self.entries:
            for row in entry["files"]:
                if row.get("pending") == "restore":
                    receipt = row["receipt"]
                    try:
                        if self.identity(Path(receipt["originalPath"])) == receipt["binIdentity"] and not os.path.lexists(receipt["binPath"]):
                            row.update(phase="restored", restoredIdentity=receipt["binIdentity"], infoCleanupPending=os.path.lexists(receipt["infoPath"]), recoveredRestore=True)
                            row.pop("pending", None)
                            self._changed()
                    except OSError:
                        pass

    def _changed(self):
        self.revision += 1
        self.dirty = True

    def _atomic_write(self, target, text):
        temporary = self.directory / ("recycle-undo-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _save(self):
        if not self.dirty:
            return True
        try:
            text = json.dumps({"version": 1, "revision": self.revision, "cursor": self.cursor,
                               "entries": self.entries}, ensure_ascii=False, separators=(",", ":"))
            # Keep an independently committed receipt journal if a reader locks
            # the main history. Startup selects the newest valid revision.
            self._atomic_write(self.journal, text)
            self._atomic_write(self.path, text)
        except (OSError, TypeError, ValueError):
            self.dirty = True
            return False
        self.dirty = False
        try:
            self.journal.unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def _state(self):
        before = self.entries[self.cursor - 1] if self.cursor else None
        after = self.entries[self.cursor] if self.cursor < len(self.entries) else None
        return {"canUndo": before is not None, "canRedo": after is not None,
                "undoLabel": before["label"] if before else None,
                "redoLabel": after["label"] if after else None,
                "undoToken": before["token"] if before else None,
                "redoToken": after["token"] if after else None,
                "undoAt": before.get("at") if before else None,
                "redoAt": after.get("at") if after else None,
                "savePending": self.dirty, "historyWarning": "Saved PC recovery history could not be fully read." if self.load_warning else None}

    def state(self):
        with self.lock:
            self._save()
            return self._state()

    def record(self, receipts: list[dict], label: str):
        if not receipts:
            return {"token": None, "undoToken": None, "historyStatus": self.state()}
        if len(receipts) > 20000:
            raise ValueError("Too many recovery receipts in one action.")
        for receipt in receipts:
            validate_receipt(receipt)
        with self.lock:
            token = uuid.uuid4().hex
            self.entries = self.entries[:self.cursor]
            self.entries.append({"token": token, "label": str(label)[:240], "at": int(time.time() * 1000), "files": [
                {"receipt": copy.deepcopy(receipt), "phase": "recycled"} for receipt in receipts]})
            self.entries = self.entries[-40:]
            self.cursor = len(self.entries)
            self._changed()
            self._save()
            return {"token": token, "undoToken": token, "historyStatus": self._state(), "savePending": self.dirty}

    def _error(self, error):
        if isinstance(error, PermissionError):
            return "Windows blocked the PC file. Close apps using it, then retry."
        if isinstance(error, FileNotFoundError):
            return "The tracked Recycle Bin file or original folder is no longer available. No other file was touched."
        return str(error) or "The PC recovery operation could not finish."

    def append(self, token, receipts: list[dict]):
        """Durably add each completed recycle to the current serialized batch."""
        for receipt in receipts:
            validate_receipt(receipt)
        with self.lock:
            if not self.entries or self.cursor != len(self.entries) or self.entries[-1]["token"] != token:
                raise ValueError("This cleanup action is no longer the current recording batch.")
            entry = self.entries[-1]
            if any(row["phase"] != "recycled" or row.get("pending") for row in entry["files"]):
                raise ValueError("Finish the current PC recovery action before adding files.")
            known = {row["receipt"]["id"] for row in entry["files"]}
            additions = [receipt for receipt in receipts if receipt["id"] not in known]
            if len(entry["files"]) + len(additions) > 20000:
                raise ValueError("Too many recovery receipts in one action.")
            entry["files"].extend({"receipt": copy.deepcopy(receipt), "phase": "recycled"} for receipt in additions)
            self._changed(); self._save()
            return {"token": token, "undoToken": token, "historyStatus": self._state(), "savePending": self.dirty}

    def _change(self, undo, token=None, validate=lambda receipt: True):
        with self.lock:
            index = self.cursor - 1 if undo else self.cursor
            if not 0 <= index < len(self.entries):
                return {"results": [], "changed": False, "complete": False, "historyStatus": self._state()}
            entry = self.entries[index]
            if token is not None and token != entry["token"]:
                raise ValueError("This PC recovery action is not the current Undo/Redo entry.")
            desired, eligible = ("restored", "recycled") if undo else ("recycled", "restored")
            results = []
            for row in entry["files"]:
                receipt = row["receipt"]
                if row["phase"] != eligible:
                    if undo and row.pop("recoveredRestore", False):
                        results.append({"path": receipt["originalPath"], "restored": True,
                                        "originalRecord": copy.deepcopy(receipt.get("originalRecord")), "recovered": True})
                        self._changed()
                    continue
                result = {"path": receipt["originalPath"], "restored" if undo else "deleted": False}
                try:
                    if not validate(copy.deepcopy(receipt)):
                        raise OSError("This tracked PC recovery action is no longer permitted for the selected file.")
                    if undo:
                        if receipt.get("capturePending"):
                            receipt = self.repair(copy.deepcopy(receipt))
                            validate_receipt(receipt)
                            row["receipt"] = receipt
                            self._changed()
                        # Persist intent before restoring so startup can resolve
                        # a completed atomic rename if the final save is blocked.
                        row["pending"] = "restore"; self._changed()
                        if not self._save():
                            raise OSError("Windows blocked saving recovery history. Retry Undo when history can be saved.")
                        outcome = self.restorer(receipt, lambda: validate(copy.deepcopy(receipt)))
                        row.update(phase="restored", **outcome)
                    else:
                        if self.identity(Path(receipt["originalPath"])) != row.get("restoredIdentity"):
                            raise OSError("The restored PC file changed or was replaced. Redo kept it untouched.")
                        if row.get("infoCleanupPending"):
                            self.cleanup(receipt)
                        new_receipt = self.recycler(Path(receipt["originalPath"]), lambda: self.identity(Path(receipt["originalPath"])) == row.get("restoredIdentity") and validate(copy.deepcopy(receipt)))
                        if receipt.get("originalRecord") is not None:
                            new_receipt["originalRecord"] = copy.deepcopy(receipt["originalRecord"])
                        row.update(receipt=new_receipt, phase="recycled", infoCleanupPending=False)
                        row.pop("restoredIdentity", None)
                    row.pop("pending", None)
                    result["restored" if undo else "deleted"] = True
                    result["originalRecord"] = copy.deepcopy(receipt.get("originalRecord"))
                    self._changed()
                    self._save()
                except Exception as error:
                    result["error"] = self._error(error)
                results.append(result)
            complete = all(row["phase"] == desired for row in entry["files"])
            if complete:
                self.cursor += -1 if undo else 1
                self._changed()
            self._save()
            count = sum(bool(result.get("restored" if undo else "deleted")) for result in results)
            return {"results": results, "changed": count > 0, "complete": complete,
                    "restored" if undo else "deleted": count,
                    "redoToken" if undo else "undoToken": entry["token"] if complete else None,
                    "historyStatus": self._state(), "savePending": self.dirty}

    def undo(self, token=None, validate=lambda receipt: True):
        return self._change(True, token, validate)

    def redo(self, token=None, validate=lambda receipt: True):
        return self._change(False, token, validate)

    def restored_records(self):
        """Recover engine ownership only for our unchanged restored receipts."""
        with self.lock:
            records = []
            for entry in self.entries:
                for row in entry["files"]:
                    receipt = row["receipt"]
                    if row["phase"] == "restored" and isinstance(receipt.get("originalRecord"), dict):
                        try:
                            if self.identity(Path(receipt["originalPath"])) == row.get("restoredIdentity"):
                                records.append({"path": receipt["originalPath"], "originalRecord": copy.deepcopy(receipt["originalRecord"])})
                        except OSError:
                            pass
            return records
