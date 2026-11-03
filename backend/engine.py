"""USB-only iPhone transfer engine and JSON-lines bridge for EyeCopy.

The pinned pymobiledevice3 7.6 API is synchronous. Every operation owns its
lockdown/AFC sockets; monitoring, scanning, and transfers run independently.
No phone identifiers, pairing records, or stack traces enter the GUI protocol.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import logging
import math
import os
import posixpath
import re
import socket
import stat as file_stat
import struct
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# The desktop launches this saved file directly; tests import it as a package.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.afc_io import read as afc_read
from backend.afc_io import iter_read as afc_iter_read
from backend.live_photos import inspect_metadata as inspect_live_metadata, group_items as group_live_photos, VERSION as LIVE_METADATA_VERSION, SUPPORTED_EXTENSIONS as LIVE_SUPPORTED_EXTENSIONS
from backend.shell_notify import notify_local_download
from backend.video_duration import inspect_duration, VERSION as VIDEO_METADATA_VERSION, SUPPORTED_EXTENSIONS as DURATION_EXTENSIONS

CHUNK_SIZE = 4 * 1024 * 1024
PUBLISH_RETRY_DELAYS = (0.1, 0.2, 0.4, 0.8, 1.0, 1.0)
STATE_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2)
LEGACY_FOLDER_DENIED = "Windows denied access to the selected folder. Choose a writable destination."
PREVIEW_LIMIT = 25 * 1024 * 1024
THUMBNAIL_LIMIT = 2 * 1024 * 1024
USBMUX_ADDRESS = os.environ.get("EYECOPY_USBMUX_ADDRESS", "127.0.0.1:27015")
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff", ".dng"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".mts"}
JOB_FIELDS = {"id", "name", "direction", "state", "bytes", "total", "speed", "eta", "error", "localPath", "deviceId", "remotePath", "remoteCleanupPending", "localPartRetained", "route", "source", "message", "destination", "awaitingStart", "cancelRequested"}
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CLOCK$", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
LOG = logging.getLogger("eyecopy")
PLAN_LIFETIME = 120
MUTATION_CACHE_WARNING = "The deletion results are accurate, but Windows temporarily blocked saving EyeCopy's history. Refresh to retry saving; files will not be deleted again."


class UserError(Exception):
    """An actionable, non-sensitive message safe to show to the user."""


class SourceChangedError(UserError):
    pass


class TransferInterrupted(Exception):
    def __init__(self, state: str):
        self.state = state


def device_key(serial: str) -> str:
    return "iphone-" + hashlib.sha256(serial.encode("utf-8")).hexdigest()[:20]


def safe_filename(value: str) -> str:
    value = unicodedata.normalize("NFC", str(value))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value).strip(" .")
    if not value or value in {".", ".."}:
        value = "untitled"
    if value.split(".", 1)[0].upper() in RESERVED_NAMES:
        value = "_" + value
    extension = Path(value).suffix[:20]
    if len(value) > 170:
        value = value[:170 - len(extension)].rstrip(" .") + extension
    return value


def remote_path(value: str, source: str) -> str:
    root = "/DCIM" if source == "photos" else "/Documents"
    if source not in {"photos", "app"} or not isinstance(value, str):
        raise UserError("Choose Camera media or a File Sharing app.")
    if "\\" in value or "\x00" in value or any(p in {".", ".."} for p in value.split("/")):
        raise UserError("The Device path is invalid.")
    value = posixpath.normpath(value)
    if value != root and not value.startswith(root + "/"):
        raise UserError("That path is outside the selected Device folder.")
    return value


def timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone().isoformat()
    if isinstance(value, (float, int)):
        if value > 10**12:
            value /= 10**9
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return str(value or "")


def signature(stat: dict) -> dict:
    return {"size": int(stat.get("st_size", 0)), "modified": timestamp(stat.get("st_mtime"))}


def local_signature(path: Path) -> dict:
    value = path.stat()
    return {"size": value.st_size, "mtimeNs": value.st_mtime_ns}


def classify(name: str, is_folder: bool = False) -> str:
    if is_folder:
        return "folder"
    extension = Path(name).suffix.lower()
    return "photo" if extension in PHOTO_EXTENSIONS else "video" if extension in VIDEO_EXTENSIONS else "file"


def unique_local_path(directory: Path, name: str) -> Path:
    name = safe_filename(name)
    base, extension = os.path.splitext(name)
    candidate = directory / name
    counter = 2
    while candidate.exists():
        candidate = directory / f"{base} ({counter}){extension}"
        counter += 1
    return candidate


def owned_part_suffixes(identifier: str) -> tuple[str, ...]:
    # Cleanup is restricted to the exact temporary filename owned by this job.
    return (f".eyecopy-{identifier}.part",)


def owned_upload_temp(value: str, directory: str, identifier: str) -> bool:
    return value in {posixpath.join(directory, suffix) for suffix in owned_part_suffixes(identifier)}


def commit_no_overwrite(part: Path, destination: Path) -> None:
    """Publish a complete file atomically while preserving an existing target."""
    if os.name == "nt":
        # Unlike POSIX rename, Windows rename fails when destination exists.
        os.rename(part, destination)
    else:
        os.link(part, destination)
        part.unlink()


def afc_seek(afc: Any, handle: int, offset: int) -> None:
    if hasattr(afc, "fseek"):
        afc.fseek(handle, offset)
    else:
        # v7.6 has FILE_SEEK but no public wrapper. Payload: handle, whence,
        # signed offset (the same layout used by libimobiledevice).
        from pymobiledevice3.services.afc import AfcOpcode
        afc._do_operation(AfcOpcode.FILE_SEEK, struct.pack("<QQq", handle, 0, offset))


def atomic_json(path: Path, data: Any) -> None:
    # Each write owns its temporary; concurrent cache writers cannot replace or
    # unlink a different writer's in-flight JSON file.
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(len(STATE_RETRY_DELAYS) + 1):
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == len(STATE_RETRY_DELAYS):
                    raise
                time.sleep(STATE_RETRY_DELAYS[attempt])
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def public_error(error: Exception) -> str:
    if isinstance(error, UserError):
        return str(error)
    kind = type(error).__name__
    if "PairingDialogResponsePending" in kind or "NotPaired" in kind:
        return "Unlock your Device, choose Pair / Trust, then accept Trust on the Device."
    if "Password" in kind or "Protected" in kind:
        return "Unlock your Device and reconnect its USB cable."
    if "DeniedPairing" in kind:
        return "Trust was declined on the Device. Choose Pair / Trust to try again."
    if isinstance(error, (ConnectionError, TimeoutError, socket.timeout, BrokenPipeError)) or any(x in kind for x in ("Connection", "NoDevice", "DeviceNotFound", "Mux")):
        return "The USB connection is unavailable. Unlock and reconnect the Device; the queue will wait for it."
    if isinstance(error, PermissionError):
        return "Windows blocked access to this file or folder. Close any app using it, then retry."
    if isinstance(error, OSError) and error.errno == 28:
        return "There is not enough free space in the destination."
    if "FileNotFound" in kind:
        return "The selected file or folder is no longer available. Refresh the catalogue."
    return "The operation could not finish. Check the connection and try again."


class Engine:
    def __init__(self, data_dir: str | Path, emit: Callable[[str, Any], None] | None = None,
                 connector: Callable | None = None, diagnostics_dir: str | Path | None = None,
                 start: bool = True, download_dir: str | Path | None = None):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.folder_rename_file = self.data_dir / "folder-rename.json"
        self.diagnostics_dir = Path(diagnostics_dir) if diagnostics_dir else Path(__file__).resolve().parent.parent / "diagnostics"
        self._emit = emit or (lambda event, data: None)
        self._connector = connector
        self.lock = threading.RLock()
        self._destructive_lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.scan_wake = threading.Event()
        self._devices: dict[str, dict] = {}
        self._serials: dict[str, str] = {}
        self._mux_devices: dict[str, Any] = {}
        self._delete_plans: dict[str, dict] = {}
        self._download_records: dict[str, dict] = {}
        self._location_history: list[dict] = []
        self._location_cursor = 0
        self._catalogues: dict[str, list[dict]] = {}
        self._apps: dict[str, list[dict]] = {}
        self._jobs: list[dict] = []
        self._requests: dict[str, str] = {}
        self._queried: dict[str, float] = {}
        self._active_job: str | None = None
        self._queue_generation = 0
        self._queue_generation_lock = threading.Lock()
        self.selected_device: str | None = None
        self.current_scope: str | None = None
        self.download_root = Path(download_dir).expanduser().resolve() if download_dir else Path.home() / "Videos" / "EyeCopy"
        self._download_layout_version = 0
        self.download_dir = str(self.download_root)
        self.connection = {"status": "offline", "message": "Connect and unlock your Device to start.", "serviceAvailable": False}
        self.scanning = False
        self._scan_request: tuple | None = None
        self._scan_generation = 0
        self._last_job_emit = 0.0
        self._last_save = 0.0
        self._save_retry_after = 0.0
        self._mutation_save_pending = False
        self._media = None
        self._thumbnail_slots = threading.BoundedSemaphore(2)
        from backend.recycle_undo import RecycleUndoHistory
        self._pc_history = RecycleUndoHistory(self.data_dir)
        self._load()
        self._initialize_global_download_root()
        with self.lock:
            self._ensure_device_directories_locked()
            self._apply_pc_history_records_locked(self._pc_history.restored_records())
            self.download_dir = self._effective_download_dir_locked()
        if start:
            for target, name in ((self._monitor_loop, "iphone-monitor"), (self._transfer_loop, "iphone-transfers"), (self._scan_loop, "iphone-catalogue")):
                threading.Thread(target=target, name=name, daemon=True).start()

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            self._devices = {d["id"]: {**d, "online": False} for d in data.get("devices", []) if isinstance(d, dict) and re.fullmatch(r"iphone-[0-9a-f]{20}", str(d.get("id", "")))}
            saved_root = data.get("downloadRootDir")
            if isinstance(saved_root, str) and Path(saved_root).is_absolute():
                self.download_root = Path(saved_root)
            self._download_layout_version = int(data.get("downloadLayoutVersion", 0))
            self._catalogues = data.get("catalogues", {})
            for items in self._catalogues.values():
                for item in self._catalogue_components(items):
                    if isinstance(item, dict) and isinstance(item.get("path"), str):
                        item.setdefault("parentPath", posixpath.dirname(item["path"]))
            self._apps = data.get("apps", {})
            self._download_records = data.get("downloadRecords", {})
            if not isinstance(self._download_records, dict):
                self._download_records = {}
            self._location_history = [entry for entry in data.get("downloadLocationHistory", [])
                                      if isinstance(entry, dict) and (entry.get("deviceId") in self._devices or entry.get("kind") == "rootPreference")
                                      and isinstance(entry.get("before"), dict) and isinstance(entry.get("after"), dict)][-40:]
            cursor = data.get("downloadLocationCursor", len(self._location_history))
            self._location_cursor = min(max(cursor if isinstance(cursor, int) else 0, 0), len(self._location_history))
            self._jobs = self._bounded_job_history(data.get("jobs", []))
            recovered = self._recover_folder_rename_locked()
            for job in self._jobs:
                if job.get("state") not in {"cancelled", "paused"} and self._published_download_exists(job):
                    self._record_local_download(job, Path(job["finalPath"]))
                    job.update(state="completed", bytes=job["total"], speed=0, eta=0, error=None)
                    job.pop("publicationPending", None)
                    job.pop("message", None)
                    recovered = True
                elif self._recoverable_failed_publication(job):
                    # One recovery attempt, never a retry loop or a user-pause
                    # override. Source metadata is checked before publication.
                    job.update(state="waiting", publicationRecoveryAttempted=True, speed=0, eta=None,
                               error=None, message="Finishing a previously downloaded file.")
                    recovered = True
                elif job.get("awaitingStart") and job.get("state") not in {"completed", "cancelled", "failed"}:
                    job.update(state="paused", speed=0, eta=None)
                elif job.get("state") in {"queued", "transferring", "waiting"}:
                    job.update(state="waiting", speed=0, eta=None)
                job.pop("retryAfter", None)
                job.pop("cleanupRetryAfter", None)
            selected = data.get("selectedDevice")
            self.selected_device = selected if isinstance(selected, str) and selected in self._devices else None
            saved_scope = data.get("currentScope")
            self.current_scope = self._validated_saved_scope(saved_scope)
            if recovered or self.current_scope != saved_scope:
                with contextlib.suppress(OSError):
                    self._save_locked()
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            LOG.warning("Saved cache could not be loaded; starting with an empty catalogue.")

    def _validated_saved_scope(self, value: Any) -> str | None:
        """Migrate one bad navigation preference without dropping saved data."""
        try:
            if not isinstance(value, str):
                raise ValueError("scope is not serialized JSON")
            parts = json.loads(value)
            if not isinstance(parts, list) or len(parts) != 4:
                raise ValueError("scope must contain four fields")
            device_id, source, app_id, path = parts
            if (not isinstance(device_id, str) or device_id not in self._devices
                    or source not in {"photos", "app"} or not isinstance(app_id, str)
                    or (source == "photos" and app_id != "")
                    or (source == "app" and (not app_id or "\x00" in app_id))):
                raise ValueError("scope identity or source is invalid")
            remote_path(path, source)
            # Keep the exact valid cache key, including legacy JSON spacing.
            return value
        except (ValueError, TypeError, UserError):
            return self.scope(self.selected_device, "photos") if self.selected_device else None

    def _save_locked(self) -> None:
        # Serial identifiers and pairing material never enter this state file.
        atomic_json(self.state_file, {"version": 1, "devices": list(self._devices.values()), "catalogues": self._catalogues,
                                    "apps": self._apps,
                                    "jobs": self._bounded_job_history(self._jobs), "selectedDevice": self.selected_device,
                                    "currentScope": self.current_scope, "downloadDir": self._effective_download_dir_locked(),
                                    "downloadRecords": self._download_records,
                                    "downloadLocationHistory": self._location_history,
                                    "downloadLocationCursor": self._location_cursor,
                                    "downloadRootDir": str(self.download_root),
                                    "downloadLayoutVersion": self._download_layout_version})
        self._last_save = time.monotonic()
        self._save_retry_after = 0.0
        self._mutation_save_pending = False
        # A directory rename and its catalogue paths are one durable action.
        # Keep the intent until a successful state commit contains the result.
        with contextlib.suppress(OSError):
            self.folder_rename_file.unlink(missing_ok=True)

    def _save_mutation_result_locked(self) -> bool:
        """A failed terminal cache commit cannot undo a physical deletion."""
        try:
            self._save_locked()
            return False
        except OSError as error:
            self._mutation_save_pending = True
            self._save_retry_after = time.monotonic() + 1.0
            LOG.warning("Deletion history save delayed: stage=state-save errno=%s winerror=%s",
                        error.errno, getattr(error, "winerror", None))
            return True

    def _flush_mutation_save_locked(self) -> bool:
        if not self._mutation_save_pending or time.monotonic() < self._save_retry_after:
            return False
        return not self._save_mutation_result_locked()

    def save(self) -> None:
        with self.lock:
            self._save_locked()

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        self.scan_wake.set()
        if self._media is not None:
            self._media.close()
        self.save()

    @staticmethod
    def scope(device_id: str, source: str, app_id: str | None = None, path: str | None = None) -> str:
        return json.dumps([device_id, source, app_id or "", path or ("/DCIM" if source == "photos" else "/Documents")], separators=(",", ":"))

    def _usb_summary(self) -> dict:
        unknown = {"negotiatedSpeed": "Unknown", "verified": False,
                   "message": "Run USB diagnostics for the selected Device to measure its negotiated connection."}
        if not self.selected_device:
            return unknown
        try:
            preferred = self.data_dir / "usb-diagnostics.json"
            report = preferred if preferred.is_file() else self.diagnostics_dir / "latest-usb-diagnostics.json"
            data = json.loads(report.read_text(encoding="utf-8-sig"))
            phones = data.get("Phones", [])
            selected = next((phone for phone in phones if phone.get("DeviceId") == self.selected_device), None)
            if not selected:
                return unknown
            usb = next((u for u in selected.get("UsbConnection", selected.get("USBConnection", []))
                        if u.get("V2QuerySucceeded") and u.get("ConnectionStatus") == 1), {})
            if not usb:
                return unknown
            age = None
            with contextlib.suppress(ValueError, TypeError):
                captured = datetime.fromisoformat(data.get("CapturedAtUtc", "").replace("Z", "+00:00"))
                age = max(0, (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds())
            online = bool(self._devices.get(self.selected_device, {}).get("online"))
            historical = not online or (age is not None and age > 60)
            message = "Last Windows USB diagnostic. Signaling rate differs from transfer throughput."
            if not online:
                message = "Previously measured USB connection; this Device is offline. Reconnect and refresh diagnostics for its current speed."
            elif historical:
                message = "Previously measured USB connection; refresh diagnostics to verify the current port and speed."
            return {"deviceId": self.selected_device, "negotiatedSpeed": usb.get("NegotiatedSpeed", "Unknown"), "signalingRate": usb.get("SignalingRate"),
                    "receiveBitsPerSecond": usb.get("ReceiveBitsPerSecond"), "transmitBitsPerSecond": usb.get("TransmitBitsPerSecond"),
                    "verified": True, "capturedAt": data.get("CapturedAtUtc"), "ageSeconds": age,
                    "historical": historical, "message": message}
        except (OSError, ValueError, TypeError):
            return unknown

    def public_jobs(self) -> list[dict]:
        with self.lock:
            return [{**{k: copy.deepcopy(v) for k, v in job.items() if k in JOB_FIELDS},
                     "localPartRetained": self._has_owned_local_part(job)} for job in self._jobs]

    def state(self) -> dict:
        with self.lock:
            self._flush_mutation_save_locked()
            self._ensure_device_directories_locked()
            self._reconcile_default_copies_locked()
            self.download_dir = self._effective_download_dir_locked()
            scope = json.loads(self.current_scope) if self.current_scope else [self.selected_device, "photos", "", "/DCIM"]
            recycle_history = self._pc_history.state()
            save_pending = self._mutation_save_pending or bool(recycle_history.get("savePending"))
            return {"devices": copy.deepcopy(list(self._devices.values())), "selectedDevice": self.selected_device,
                    "currentSource": scope[1], "currentAppId": scope[2] or None, "currentPath": scope[3],
                    "connection": copy.deepcopy(self.connection), "catalogue": copy.deepcopy(self._catalogues.get(self.current_scope or "", [])),
                    "apps": copy.deepcopy(self._apps.get(self.selected_device or "", [])), "jobs": self.public_jobs(),
                    "downloadDir": self.download_dir, "usb": self._usb_summary(), "scanning": self.scanning,
                    "downloadRoot": str(self.download_root), "rootPath": str(self.download_root),
                    "defaultBaseDir": str(self.download_root.parent),
                    "locationHistory": self._location_history_state_locked(),
                    "recycleHistory": recycle_history,
                    "stateSavePending": save_pending,
                    "cacheWarning": MUTATION_CACHE_WARNING if save_pending else None}

    def emit_state(self) -> None:
        self._emit("state", self.state())

    def _publish_jobs(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_job_emit >= 0.15:
            self._last_job_emit = now
            self._emit("jobs", self.public_jobs())
        with self.lock:
            if (force or now - self._last_save >= 1.0) and now >= self._save_retry_after:
                try:
                    self._save_locked()
                except OSError as error:
                    # The job and owned partial path were persisted before its
                    # first byte. A temporary progress-cache lock must not fail
                    # a media copy or kill the transfer worker in its finally.
                    self._save_retry_after = time.monotonic() + 1.0
                    LOG.warning("Transfer cache save delayed: job=%s stage=state-save errno=%s winerror=%s",
                                self._active_job, error.errno, getattr(error, "winerror", None))

    def _ensure_device_directories_locked(self) -> None:
        used = set()
        assigned = {os.path.normcase(str(Path(device["defaultDownloadDir"]).expanduser().absolute()))
                    for device in self._devices.values() if device.get("defaultDownloadDir")
                    and (device.get("defaultDownloadKind") == "custom" or not self._legacy_numbered_default(device["defaultDownloadDir"]))}
        for device in self._devices.values():
            number = device.get("downloadNumber")
            if isinstance(number, int) and not isinstance(number, bool) and number > 0 and number not in used:
                used.add(number)
            else:
                device.pop("downloadNumber", None)
        for device in self._devices.values():
            if "downloadNumber" not in device:
                number = 1
                while number in used:
                    number += 1
                device["downloadNumber"] = number
                used.add(number)
            old_default = device.get("defaultDownloadDir")
            if old_default and (device.get("defaultDownloadKind") == "custom" or not self._legacy_numbered_default(old_default)):
                default = str(Path(old_default).expanduser().absolute())
                device.setdefault("defaultDownloadKind", "custom" if Path(default).parent != self.download_root else "generated")
                device.setdefault("defaultBaseDir", str(Path(default).parent))
                # Existing custom exact-folder settings are preserved until the
                # user chooses a parent or explicitly renames their phone.
                device.setdefault("defaultPhoneNamed", device["defaultDownloadKind"] == "generated")
                if device["defaultPhoneNamed"]:
                    expected = Path(device["defaultBaseDir"]) / self._phone_folder_name(device)
                    if str(expected) != default:
                        # This routine already owns the state lock. Never wait
                        # for a mutation which may be waiting for that lock.
                        # Explicit rename/undo owns the mutation lock first.
                        if self._destructive_lock.acquire(blocking=False):
                            try:
                                self._rename_download_folder_locked(device["id"], Path(default), expected)
                                default = str(expected)
                                device.pop("downloadFolderWarning", None)
                            except UserError as error:
                                device["downloadFolderWarning"] = str(error)
                            finally:
                                self._destructive_lock.release()
                        else:
                            device["downloadFolderWarning"] = "The Device folder name will update after the current file operation finishes."
                    else:
                        device.pop("downloadFolderWarning", None)
            else:
                stem = self._phone_folder_name(device)
                slug, suffix = stem, 2
                while os.path.normcase(str(self.download_root / slug)) in assigned:
                    slug = f"{stem} ({suffix})"
                    suffix += 1
                default = str(self.download_root / slug)
                device["defaultDownloadKind"] = "generated"
                device["downloadSlug"] = slug
                device["defaultBaseDir"] = str(self.download_root)
                device["defaultPhoneNamed"] = slug == stem
                if old_default:
                    device["previousDownloadDir"] = old_default
            assigned.add(os.path.normcase(default))
            device["defaultDownloadDir"] = default
            if not device.get("downloadDir") or device.get("downloadDir") == old_default:
                device["downloadDir"] = default

    @staticmethod
    def _phone_folder_name(device: dict) -> str:
        return safe_filename(str(device.get("alias") or device.get("name") or "Device"))

    def _check_distinct_device_default_locked(self, identifier: str, directory: Path) -> None:
        if any(other.get("id") != identifier and other.get("defaultDownloadDir")
               and Path(other["defaultDownloadDir"]) == directory for other in self._devices.values()):
            raise UserError("Another Device already uses that default folder. Rename one of these Devices.")

    @staticmethod
    def _plain_directory_path(path: Path, *, require_directory: bool = True) -> bool:
        try:
            if not path.is_absolute():
                return False
            for component in [path, *path.parents]:
                stat = component.lstat()
                if component.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                    return False
            return path.is_dir() if require_directory else True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _repath_under(value: Any, old: Path, new: Path) -> Any:
        if not isinstance(value, str):
            return value
        try:
            current = Path(value)
            return str(new / current.relative_to(old)) if current.is_absolute() and current.is_relative_to(old) else value
        except (ValueError, OSError):
            return value

    def _repath_download_state_locked(self, old: Path, new: Path) -> None:
        records = {}
        for key, record in self._download_records.items():
            rewritten = copy.deepcopy(record)
            rewritten["path"] = self._repath_under(record.get("path"), old, new)
            records[self._repath_under(key, old, new)] = rewritten
        self._download_records = records
        for items in self._catalogues.values():
            for item in self._catalogue_components(items):
                if item.get("localPath"):
                    item["localPath"] = self._repath_under(item["localPath"], old, new)
        for job in self._jobs:
            for key in ("localPath", "finalPath", "partPath", "destination", "sourcePath"):
                if job.get(key):
                    job[key] = self._repath_under(job[key], old, new)

    def _recover_folder_rename_locked(self) -> bool:
        try:
            journal = json.loads(self.folder_rename_file.read_text(encoding="utf-8"))
            device_id = journal["deviceId"]
            device = self._devices[device_id]
            old, new = Path(journal["old"]), Path(journal["new"])
            identity = journal["directoryIdentity"]
            if (old.parent != new.parent or not old.is_absolute() or not new.is_absolute()
                    or journal["after"].get("defaultDownloadDir") != str(new)
                    or not self._plain_directory_path(new)
                    or self._file_identity(new).get("inode") != identity.get("inode")
                    or self._file_identity(new).get("device") != identity.get("device")):
                return False
            # Case-only Windows renames leave both spellings addressable. Their
            # identity proves the one directory; distinct existing roots reject.
            if old.exists() and not os.path.samefile(old, new):
                return False
            self._repath_download_state_locked(old, new)
            device.update(journal["after"])
            mode = journal.get("mode", "automatic")
            if mode == "new":
                entry = journal["historyEntry"]
                if not any(existing.get("id") == entry["id"] for existing in self._location_history):
                    self._location_history = self._location_history[:self._location_cursor]
                    self._location_history.append(entry)
                    self._location_history = self._location_history[-40:]
                    self._location_cursor = len(self._location_history)
            elif mode in {"undo", "redo"}:
                self._location_cursor = min(max(journal["historyCursor"], 0), len(self._location_history))
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError, TypeError, KeyError):
            LOG.warning("A download-folder rename recovery could not be verified; existing folders were kept.")
            return False

    def _rename_download_folder_locked(self, device_id: str, old: Path, new: Path, *,
                                       target_preferences: dict | None = None, journal_details: dict | None = None) -> bool:
        """Rename one ordinary sibling directory; never merge or overwrite it."""
        if str(old) == str(new):
            return False
        device = self._devices[device_id]
        base = Path(device.get("defaultBaseDir") or old.parent)
        if not old.is_absolute() or old.parent != base or new.parent != base or new.name in {"", ".", ".."}:
            raise UserError("The Device download folder cannot be renamed outside its chosen parent.")
        if any(other.get("id") != device_id and other.get("defaultDownloadDir")
               and Path(other["defaultDownloadDir"]) == new for other in self._devices.values()):
            raise UserError("Another Device already uses that folder name. Choose a different Device name.")
        active = next((job for job in self._jobs if job.get("id") == self._active_job), None)
        if active and active.get("state") not in {"completed", "failed", "cancelled"} and (
                active.get("deviceId") == device_id or any(self._repath_under(active.get(key), old, new) != active.get(key)
                                                         for key in ("destination", "sourcePath"))):
            raise UserError("Pause the active transfer before renaming this Device's download folder.")
        try:
            exists = old.exists()
            if not exists:
                if new.exists():
                    raise UserError("A folder already exists with the new Device name. It was kept; choose another name.")
                self._repath_download_state_locked(old, new)
                return False
            if not self._plain_directory_path(old) or not self._plain_directory_path(base):
                raise UserError("The download folder or its parent is a link or is unavailable. Choose an ordinary local parent folder.")
            same = new.exists() and os.path.samefile(old, new)
            if new.exists() and not same:
                raise UserError("A folder already exists with the new Device name. Neither folder was changed; choose another name.")
            after = target_preferences or {**self._location_preferences(device), "defaultDownloadDir": str(new),
                                           "downloadDir": str(new) if device.get("downloadDir") == str(old) else device.get("downloadDir")}
            atomic_json(self.folder_rename_file, {"deviceId": device_id, "old": str(old), "new": str(new),
                                                 "directoryIdentity": self._file_identity(old), "after": after,
                                                 **(journal_details or {})})
            # MoveFile supports case-only sibling renames. POSIX rename has an
            # overwrite behavior, so its existing-target branch was rejected.
            os.rename(old, new)
        except OSError as error:
            raise UserError("Windows could not rename this Device's download folder. Close apps using it and try again; existing files were kept.") from error
        self._repath_download_state_locked(old, new)
        return True

    def _legacy_numbered_default(self, value: str) -> bool:
        try:
            path = Path(value).expanduser().absolute()
            return path.parent == self.download_root and re.fullmatch(r"iphone-[1-9][0-9]*", path.name) is not None
        except (OSError, ValueError, TypeError):
            return False

    def _default_records_by_item_locked(self) -> dict[str, list[dict]]:
        """Inspect only current default folders; custom drop destinations stay private."""
        return self._validated_pc_records(self._download_records.values(), self._devices)

    def _validated_pc_records(self, candidates, devices) -> dict[str, list[dict]]:
        """Read-only verification also accepts detached snapshots off the state lock."""
        records: dict[str, list[dict]] = {}
        roots: dict[Path, bool] = {}
        for record in candidates:
            device = devices.get(record.get("deviceId"))
            if not device:
                continue
            try:
                root = Path(device["defaultDownloadDir"])
                # Test parent before any filesystem call: never stat an external
                # drag destination or a former default while refreshing badges.
                path = Path(record["path"])
                if path.parent != root:
                    continue
                if root not in roots:
                    roots[root] = self._plain_directory_path(root)
                if not roots[root]:
                    continue
                # Badge refresh is read-only. Validate each root once and each
                # file from one lstat; cleanup still rechecks its complete path
                # immediately before the destructive Shell callback.
                current = path.lstat()
                identity = {"size": current.st_size, "inode": current.st_ino, "device": current.st_dev}
                if (not file_stat.S_ISREG(current.st_mode) or getattr(current, "st_file_attributes", 0) & 0x400
                        or identity != record.get("identity") or current.st_mtime_ns != record.get("modifiedNs")):
                    continue
                records.setdefault(record["itemId"], []).append(record)
            except (KeyError, OSError, TypeError, ValueError):
                continue
        return records

    @staticmethod
    def _record_matches_item(record: dict, item: dict) -> bool:
        expected = record.get("sourceSignature")
        return (record.get("deviceId") == item.get("deviceId") and
                record.get("identity", {}).get("size") == item.get("size") and
                (expected is None or expected == {"size": item.get("size"), "modified": item.get("modified")}))

    def _reconcile_default_copies_locked(self) -> bool:
        records = self._default_records_by_item_locked()
        changed = False
        for items in self._catalogues.values():
            for item in self._catalogue_components(items):
                matching = next((record for record in records.get(item.get("id"), [])
                                 if self._record_matches_item(record, item)), None)
                path = matching["path"] if matching and item.get("kind") != "folder" else None
                if item.get("localPath") != path:
                    item.pop("localPath", None)
                    if path:
                        item["localPath"] = path
                    changed = True
        return changed

    def _global_location_snapshot_locked(self) -> dict:
        return {"rootPath": str(self.download_root),
                "devices": {identifier: self._location_preferences(device) for identifier, device in self._devices.items()}}

    def _global_location_target_locked(self, root: Path) -> dict:
        result, used = {}, set()
        for identifier, device in self._devices.items():
            name, suffix = self._phone_folder_name(device), 2
            leaf = name
            while os.path.normcase(str(root / leaf)) in used:
                leaf = f"{name} ({suffix})"
                suffix += 1
            destination = root / leaf
            used.add(os.path.normcase(str(destination)))
            preferences = self._location_preferences(device)
            preferences.update(defaultDownloadDir=str(destination), downloadDir=str(destination),
                               defaultDownloadKind="global", defaultBaseDir=str(root),
                               defaultPhoneNamed=leaf == name, downloadSlug=leaf)
            result[identifier] = preferences
        return {"rootPath": str(root), "devices": result}

    def _validate_global_root(self, root: Path) -> None:
        if root.is_relative_to(self.data_dir) or self.data_dir.is_relative_to(root):
            raise UserError("The EyeCopy download folder must be separate from application data and cache. Existing files were kept.")
        if root.exists() and not root.is_dir():
            raise UserError("That EyeCopy location is a file. Choose another parent folder.")
        existing = root
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if not self._plain_directory_path(existing):
            raise UserError("Choose an ordinary EyeCopy folder and parent, not a link or junction. Existing files were kept.")

    def _apply_global_location_locked(self, snapshot: dict) -> None:
        self.download_root = Path(snapshot["rootPath"])
        for identifier, preferences in snapshot["devices"].items():
            if identifier in self._devices:
                self._devices[identifier].update(preferences)
        self._download_layout_version = 2
        self._reconcile_default_copies_locked()
        self.download_dir = self._effective_download_dir_locked()

    def _set_global_download_root(self, parent: str) -> dict:
        if not isinstance(parent, str) or not parent:
            raise UserError("Choose a parent folder for EyeCopy.")
        target = Path(parent).expanduser().resolve() / "EyeCopy"
        self._validate_global_root(target)
        with self._destructive_lock:
            with self.lock:
                before = self._global_location_snapshot_locked()
                after = self._global_location_target_locked(target)
                if before == after:
                    return {"changed": False, "rootPath": str(target), "path": str(target), "basePath": str(target.parent)}
                history_before, cursor_before = copy.deepcopy(self._location_history), self._location_cursor
                entry = {"id": uuid.uuid4().hex, "kind": "rootPreference", "deviceId": None,
                         "before": {"rootPath": before["rootPath"]}, "after": {"rootPath": after["rootPath"]},
                         "label": "Change default folder", "createdAt": time.time()}
                self._apply_global_location_locked(after)
                self._location_history = self._location_history[:self._location_cursor]
                self._location_history.append(entry)
                self._location_history = self._location_history[-40:]
                self._location_cursor = len(self._location_history)
                try:
                    self._save_locked()
                except OSError:
                    self._apply_global_location_locked(before)
                    self._location_history, self._location_cursor = history_before, cursor_before
                    raise
        self.emit_state()
        return {"changed": True, "historyId": entry["id"], "rootPath": str(target), "path": str(target),
                "basePath": str(target.parent)}

    def _initialize_global_download_root(self) -> None:
        if self._download_layout_version >= 2:
            return
        with self.lock:
            # Initial application-name adoption is a preference change only.
            # Old media, folders, jobs and ownership records retain their paths.
            target = self._global_location_target_locked(self.download_root)
            self._apply_global_location_locked(target)
            # Earlier per-phone default choices cannot undo one device out of
            # the new app-wide layout. Start a fresh coherent preference history.
            self._location_history, self._location_cursor = [], 0
            self._save_mutation_result_locked()

    def _location_history_state_locked(self) -> dict:
        before = self._location_history[self._location_cursor - 1] if self._location_cursor else None
        after = self._location_history[self._location_cursor] if self._location_cursor < len(self._location_history) else None
        return {"canUndo": before is not None, "canRedo": after is not None,
                "undoLabel": before.get("label") if before else None,
                "redoLabel": after.get("label") if after else None,
                "undoId": before.get("id") if before else None,
                "redoId": after.get("id") if after else None,
                "undoAt": before.get("createdAt", 0) if before else None,
                "redoAt": after.get("createdAt", 0) if after else None}

    @staticmethod
    def _location_preferences(device: dict) -> dict:
        return {key: device.get(key) for key in ("defaultDownloadDir", "downloadDir", "defaultDownloadKind", "downloadSlug",
                                                "defaultBaseDir", "defaultPhoneNamed", "alias")}

    def _save_location_change_locked(self, device_id: str, before: dict, label: str, *,
                                    entry_id: str | None = None, created_at: float | None = None) -> dict | None:
        after = self._location_preferences(self._devices[device_id])
        if before == after:
            return None
        self._location_history = self._location_history[:self._location_cursor]
        entry = {"id": entry_id or uuid.uuid4().hex, "deviceId": device_id, "before": before, "after": after, "label": label,
                 "createdAt": created_at if created_at is not None else time.time()}
        self._location_history.append(entry)
        self._location_history = self._location_history[-40:]
        self._location_cursor = len(self._location_history)
        return entry

    def _change_location_history(self, undo: bool, history_id: str | None = None) -> dict:
        with self.lock:
            index = self._location_cursor - 1 if undo else self._location_cursor
            root_entry = self._location_history[index] if 0 <= index < len(self._location_history) else None
            if root_entry and root_entry.get("kind") == "rootPreference":
                if history_id is not None and root_entry.get("id") != history_id:
                    raise UserError("This EyeCopy-folder action is no longer the next history entry. Refresh before undoing or redoing it.")
                target = copy.deepcopy(root_entry["before" if undo else "after"])
            else:
                target = None
        if target is not None:
            self._validate_global_root(Path(target["rootPath"]))
            with self.lock:
                before = self._global_location_snapshot_locked()
                old_cursor = self._location_cursor
                self._apply_global_location_locked(self._global_location_target_locked(Path(target["rootPath"])))
                self._location_cursor += -1 if undo else 1
                try:
                    self._save_locked()
                except OSError:
                    self._apply_global_location_locked(before)
                    self._location_cursor = old_cursor
                    raise
            self.emit_state()
            return {"changed": True, "historyId": root_entry["id"], "rootPath": str(self.download_root),
                    "path": str(self.download_root), "basePath": str(self.download_root.parent)}
        with self.lock:
            index = self._location_cursor - 1 if undo else self._location_cursor
            if index < 0 or index >= len(self._location_history):
                return {"changed": False, **self._location_history_state_locked()}
            entry = self._location_history[index]
            if history_id is not None and entry.get("id") != history_id:
                raise UserError("This download-folder action is no longer the next history entry. Refresh before undoing or redoing it.")
            device = self._devices[self._require_device(entry["deviceId"])]
            old_preferences = self._location_preferences(device)
            old_cursor = self._location_cursor
            preferences = entry["before" if undo else "after"]
            renamed = bool(entry.get("renameFolder"))
            if renamed:
                self._rename_download_folder_locked(entry["deviceId"], Path(old_preferences["defaultDownloadDir"]),
                                                    Path(preferences["defaultDownloadDir"]), target_preferences=preferences,
                                                    journal_details={"mode": "undo" if undo else "redo",
                                                                     "historyCursor": old_cursor + (-1 if undo else 1)})
            for key, value in preferences.items():
                if value is None:
                    device.pop(key, None)
                else:
                    device[key] = value
            self._location_cursor += -1 if undo else 1
            self._reconcile_default_copies_locked()
            try:
                self._save_locked()
            except OSError:
                if renamed:
                    self._rename_download_folder_locked(entry["deviceId"], Path(preferences["defaultDownloadDir"]),
                                                        Path(old_preferences["defaultDownloadDir"]), target_preferences=old_preferences)
                device.update(old_preferences)
                self._location_cursor = old_cursor
                self._reconcile_default_copies_locked()
                with contextlib.suppress(OSError):
                    self.folder_rename_file.unlink(missing_ok=True)
                raise
        self.emit_state()
        return {"changed": True, "deviceId": entry["deviceId"], "path": device["defaultDownloadDir"]}

    def _destination_device_locked(self, identifier: str | None = None) -> str | None:
        if identifier:
            return self._require_device(identifier)
        if self.selected_device and self._devices.get(self.selected_device, {}).get("online"):
            return self.selected_device
        online = [key for key, device in self._devices.items() if device.get("online")]
        return next(iter(sorted(online or self._devices, key=lambda key: (
            str(self._devices[key].get("alias") or self._devices[key].get("name") or "Device").casefold(), key))), None)

    def _effective_download_dir_locked(self, identifier: str | None = None) -> str:
        self._ensure_device_directories_locked()
        identifier = self._destination_device_locked(identifier)
        return self._devices[identifier]["downloadDir"] if identifier else str(self.download_root)

    def _require_device(self, device_id: str | None) -> str:
        device_id = device_id or self.selected_device
        with self.lock:
            if device_id not in self._devices:
                raise UserError("Choose a remembered or connected Device first.")
        return device_id

    def _available(self, device_id: str) -> bool:
        with self.lock:
            return bool(self._devices.get(device_id, {}).get("online"))

    @contextlib.contextmanager
    def _lockdown(self, device_id: str, autopair: bool = False):
        from pymobiledevice3.lockdown import create_using_usbmux
        with self.lock:
            serial = self._serials.get(device_id)
        if not serial:
            raise ConnectionError("offline")
        lockdown = create_using_usbmux(serial=serial, connection_type="USB", autopair=autopair,
                                     pair_timeout=15 if autopair else 0, usbmux_address=USBMUX_ADDRESS)
        try:
            lockdown.service.socket.settimeout(12)
            yield lockdown
        finally:
            lockdown.close()

    @contextlib.contextmanager
    def _afc(self, device_id: str, source: str, app_id: str | None = None):
        if self._connector:
            with self._connector(device_id, source, app_id) as afc:
                yield afc
            return
        from pymobiledevice3.services.afc import AfcService
        from pymobiledevice3.services.house_arrest import HouseArrestService
        with self._lockdown(device_id) as lockdown:
            if not lockdown.paired:
                raise UserError("Unlock the Device and choose Pair / Trust before transferring.")
            if source == "app":
                self._require_app(device_id, app_id)
                service = HouseArrestService(lockdown, app_id, documents_only=True)
            elif source == "photos":
                service = AfcService(lockdown)
            else:
                raise UserError("Choose Camera media or a File Sharing app.")
            try:
                service.service.socket.settimeout(12)
                yield service
            finally:
                service.close()

    def _require_app(self, device_id: str, app_id: str | None) -> None:
        with self.lock:
            if not app_id or not any(a["id"] == app_id for a in self._apps.get(device_id, [])):
                raise UserError("Refresh File Sharing apps and choose one before transferring documents.")

    def _monitor_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.poll_devices()
                with self.lock:
                    saved = self._flush_mutation_save_locked()
                if saved:
                    self.emit_state()
            except Exception as error:
                LOG.warning("Device monitoring: %s", type(error).__name__)
            self.stop.wait(3)

    def poll_devices(self) -> None:
        old_state = None
        with self.lock:
            old_state = (copy.deepcopy(self._devices), copy.deepcopy(self.connection), self.selected_device)
        available = False
        try:
            host, port = USBMUX_ADDRESS.rsplit(":", 1)
            with socket.create_connection((host, int(port)), timeout=1):
                available = True
        except OSError:
            pass
        observed: dict[str, str] = {}
        mux_devices = {}
        statuses: dict[str, tuple[str, str]] = {}
        discovery_failed = False
        if available:
            try:
                from pymobiledevice3.usbmux import list_devices
                for mux_device in list_devices(USBMUX_ADDRESS):
                    if mux_device.connection_type != "USB":
                        continue
                    identifier = device_key(mux_device.serial)
                    observed[identifier] = mux_device.serial
                    mux_devices[identifier] = mux_device
            except Exception:
                discovery_failed = True
        with self.lock:
            self._serials = observed
            self._mux_devices = mux_devices
            for identifier, device in self._devices.items():
                if identifier not in observed:
                    device["online"] = False
            if self.selected_device is None and observed:
                self.selected_device = next(iter(observed))
                self.current_scope = self.scope(self.selected_device, "photos")
        for identifier in observed:
            with self.lock:
                cached = self._devices.get(identifier)
                if cached and cached.get("online") and time.monotonic() - self._queried.get(identifier, 0) < 30:
                    continue
            try:
                with self._lockdown(identifier) as lockdown:
                    values = lockdown.all_values
                    record = {"id": identifier, "name": str(values.get("DeviceName") or "Device"),
                              "alias": cached.get("alias") if cached else None, "online": bool(lockdown.paired),
                              "battery": cached.get("battery") if cached else None,
                              "storageTotal": cached.get("storageTotal") if cached else None,
                              "storageFree": cached.get("storageFree") if cached else None,
                              "productVersion": values.get("ProductVersion"), "productType": values.get("ProductType")}
                    if lockdown.paired:
                        with contextlib.suppress(Exception):
                            power = lockdown.get_value(domain="com.apple.mobile.battery") or {}
                            record["battery"] = power.get("BatteryCurrentCapacity")
                        with contextlib.suppress(Exception):
                            storage = lockdown.get_value(domain="com.apple.disk_usage") or {}
                            record["storageTotal"] = storage.get("TotalDiskCapacity")
                            record["storageFree"] = storage.get("AmountDataAvailable", storage.get("TotalDataAvailable"))
                    else:
                        statuses[identifier] = ("trust_required", "Unlock this Device, choose Pair / Trust, and accept Trust on its screen.")
                    with self.lock:
                        remembered = self._devices.get(identifier, {})
                        record["alias"] = remembered.get("alias", record["alias"])
                        for field in ("downloadNumber", "defaultDownloadDir", "downloadDir", "filesAppId", "defaultDownloadKind", "downloadSlug",
                                      "previousDownloadDir", "defaultBaseDir", "defaultPhoneNamed"):
                            if field in remembered:
                                record[field] = remembered[field]
                        self._devices[identifier] = record
                        self._queried[identifier] = time.monotonic()
            except Exception as error:
                kind = type(error).__name__
                status = "locked" if "Password" in kind else "trust_required" if "Pair" in kind else "offline"
                statuses[identifier] = (status, public_error(error))
                with self.lock:
                    self._devices.setdefault(identifier, {"id": identifier, "name": "Device", "alias": None, "battery": None,
                                                         "storageTotal": None, "storageFree": None, "productVersion": None, "productType": None})["online"] = False
        with self.lock:
            self._ensure_device_directories_locked()
            if not available:
                status, message = "missing_service", "Apple USB transport is unavailable. Open Apple Devices once, then unlock and reconnect your Device. Use Setup guide if Apple Devices is not installed."
            elif discovery_failed:
                status, message = "offline", "Apple USB discovery did not respond. Unlock and reconnect the Device, or restart Apple Mobile Device Service."
            elif self.selected_device and self._devices.get(self.selected_device, {}).get("online"):
                status, message = "online", "Connected over USB. Camera media and File Sharing app documents are available."
            elif self.selected_device in statuses:
                status, message = statuses[self.selected_device]
            elif observed:
                status, message = "offline", "Select the connected Device, then unlock it and accept Trust if asked."
            else:
                status, message = "offline", "Device offline. Cached files and your queue remain available. Reconnect to continue."
            self.connection = {"status": status, "message": message, "serviceAvailable": available}
            changed = old_state != (self._devices, self.connection, self.selected_device)
            if changed:
                self._save_locked()
        if changed:
            self._emit("connection", copy.deepcopy(self.connection))
            self.emit_state()
            self.wake.set()

    def list_apps(self, device_id: str) -> list[dict]:
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
        with self._lockdown(device_id) as lockdown:
            if not lockdown.paired:
                raise UserError("Unlock and pair the Device before listing its File Sharing apps.")
            with InstallationProxyService(lockdown) as service:
                service.service.socket.settimeout(12)
                result = service.browse({"ApplicationType": "Any"}, attributes=["CFBundleIdentifier", "CFBundleDisplayName", "CFBundleName", "UIFileSharingEnabled", "LSSupportsOpeningDocumentsInPlace"])
        apps = [{"id": a["CFBundleIdentifier"], "bundleId": a["CFBundleIdentifier"],
                 "name": a.get("CFBundleDisplayName") or a.get("CFBundleName") or a["CFBundleIdentifier"],
                 "filesVisible": bool(a.get("LSSupportsOpeningDocumentsInPlace"))}
                for a in result if a.get("UIFileSharingEnabled") and a.get("CFBundleIdentifier")]
        apps.sort(key=lambda a: a["name"].casefold())
        with self.lock:
            self._apps[device_id] = apps
            self._save_locked()
        self.emit_state()
        return apps

    def files_home(self, device_id: str, refresh: bool = False) -> dict:
        if refresh or device_id not in self._apps:
            if self._available(device_id):
                self.list_apps(device_id)
        with self.lock:
            apps = copy.deepcopy(self._apps.get(device_id, []))
            remembered = self._devices[device_id].get("filesAppId")
            native = next((app for app in apps if app.get("id") in {
                "com.eyecopy.mobile", "com.eyecopy.iphone", "com.eyecopy.app", "com.eyecopy.EyeCopy"
            } and app.get("name") == "EyeCopy"), None)
            app = native or next((app for app in apps if app.get("id") == remembered), None)
            is_native = bool(native and app == native)
            path = "/Documents" if is_native else "/Documents/EyeCopy" if app else None
            return {"deviceId": device_id, "appId": app["id"] if app else None, "path": path,
                    "label": "EyeCopy" if is_native else f"{app['name']} / EyeCopy" if app else "Choose a Files app location",
                    "nativeEyeCopy": is_native, "requiresChoice": app is None, "apps": apps,
                    "message": ("EyeCopy companion documents are available over USB." if is_native else
                                "Uploads go into this app's EyeCopy folder. iOS restricts USB access to app documents; the On My iPhone root and Safari Downloads are unavailable.")}

    def configure_files_home(self, device_id: str, app_id: str) -> dict:
        self._require_app(device_id, app_id)
        with self.lock:
            self._devices[device_id]["filesAppId"] = app_id
            self._save_locked()
        self.emit_state()
        return self.files_home(device_id)

    def ensure_files_home(self, device_id: str) -> dict:
        home = self.files_home(device_id)
        if home["requiresChoice"]:
            return home
        self._require_app(device_id, home["appId"])
        with self._afc(device_id, "app", home["appId"]) as afc:
            self._check_directory(afc, "/Documents", "app")
            if home["path"] != "/Documents":
                if not afc.exists(home["path"]):
                    afc.makedirs(home["path"])
                self._check_directory(afc, home["path"], "app")
        return home

    def create_folder(self, device_id: str, app_id: str, path: str, name: str) -> dict:
        self._require_app(device_id, app_id)
        path = remote_path(path, "app")
        # Reject rather than silently rewrite folder names used in confirmation.
        if (not isinstance(name, str) or not self._leaf(name) or name != name.strip(" .")
                or len(name) > 170 or any(ord(c) < 32 or c in ':<>"|?*' for c in name)
                or name.split(".", 1)[0].upper() in RESERVED_NAMES):
            raise UserError("Enter a folder name without reserved characters, trailing spaces, or dots.")
        target = remote_path(posixpath.join(path, name), "app")
        with self._afc(device_id, "app", app_id) as afc:
            self._check_directory(afc, path, "app")
            if afc.exists(target):
                raise UserError("A file or folder with that name already exists. Choose another name.")
            afc.makedirs(target)
            self._check_directory(afc, target, "app")
        return {"created": True, "path": target, "name": name}

    def _reject_busy_folder(self, item: dict) -> None:
        path = item["path"]
        with self.lock:
            for job in self._jobs:
                if job.get("deviceId") != item["deviceId"] or job.get("state") in {"completed", "cancelled", "failed"}:
                    continue
                source = job.get("item", {})
                if (source.get("source") == "app" and source.get("appId") == item["appId"]
                        and (source.get("path") == path or str(source.get("path", "")).startswith(path + "/"))):
                    raise UserError("Wait for transfers inside this folder to finish before deleting it.")
                if job.get("appId") == item["appId"] and (job.get("remoteDir") == path
                        or str(job.get("remoteDir", "")).startswith(path + "/")):
                    raise UserError("Wait for uploads inside this folder to finish before deleting it.")

    def preview_phone_folder_delete(self, supplied_items: list[dict]) -> dict:
        from backend.phone_folders import snapshot
        if not isinstance(supplied_items, list) or not 0 < len(supplied_items) <= 5000:
            raise UserError("Select folders or files in a shared Files App directory.")
        resolved = [self._resolve_item(item) for item in supplied_items]
        unique = {item["id"]: item for item in resolved}
        selected = []
        for item in sorted(unique.values(), key=lambda value: len(value["path"])):
            if item.get("source") != "app":
                raise UserError("Folder deletion is only available in shared Files App documents.")
            self._require_app(item["deviceId"], item.get("appId"))
            item["path"] = remote_path(item["path"], "app")
            if any(parent["deviceId"] == item["deviceId"] and parent["appId"] == item["appId"]
                   and item["path"].startswith(parent["path"] + "/") for parent in selected):
                continue
            selected.append(item)
        files = []
        for item in selected:
            self._reject_busy_folder(item)
            try:
                with self._afc(item["deviceId"], "app", item["appId"]) as afc:
                    approved = snapshot(afc, item["path"])
            except ValueError as error:
                raise UserError(str(error)) from error
            if (item["kind"] == "folder") != (approved["entries"][0]["signature"]["kind"] == "S_IFDIR"):
                raise SourceChangedError("This Device entry changed. Refresh and review the selection again.")
            files.append({"id": item["id"], "name": item["name"], "path": item["path"], "source": "app",
                          "appId": item["appId"], "deviceId": item["deviceId"], "kind": item["kind"],
                          "size": approved["bytes"], "containedFiles": approved["fileCount"],
                          "containedFolders": approved["folderCount"], "signature": approved})
            if sum(len(file["signature"]["entries"]) for file in files) > 5000:
                raise UserError("This selection is too large to delete in one action. Delete smaller subfolders first.")
        count_files = sum(item["containedFiles"] for item in files)
        count_folders = sum(item["containedFolders"] for item in files)
        return self._store_delete_plan("phone-folders", files, permanent=True,
            warning=f"Permanently delete this selection and its contents ({count_files} files and {count_folders} folders) from the Device? Recently Deleted and Undo are unavailable. All PC copies are kept.")

    def confirm_phone_folder_delete(self, token: str) -> dict:
        from backend.phone_folders import validate, remove
        plan = self._take_delete_plan(token, "phone-folders")
        # Validate the entire selection before the first irreversible action.
        for item in plan["files"]:
            current = self._resolve_item({"id": item["id"]}, item["deviceId"])
            if any(current.get(key) != item.get(key) for key in ("source", "path", "appId", "kind")):
                raise SourceChangedError("The Device selection changed. Refresh and review deletion again.")
            self._reject_busy_folder(item)
            try:
                with self._afc(item["deviceId"], "app", item["appId"]) as afc:
                    validate(afc, item["signature"])
            except ValueError as error:
                raise SourceChangedError(str(error)) from error
        with self.lock:
            self._scan_generation += 1
            self._scan_request = None
            self.scanning = False
        results, removed_keys = [], set()
        for item in plan["files"]:
            result = {"id": item["id"], "name": item["name"], "path": item["path"], "deleted": False}
            try:
                self._reject_busy_folder(item)
                with self._afc(item["deviceId"], "app", item["appId"]) as afc:
                    outcome = remove(afc, item["signature"])
                removed_keys.update((item["deviceId"], item["appId"], path) for path in outcome["removedPaths"])
                result["deleted"] = outcome["deleted"]
                result["removedCount"] = len(outcome["removedPaths"])
                if not outcome["deleted"]:
                    result["error"] = outcome["error"]
            except Exception as error:
                result["error"] = public_error(error)
            results.append(result)
        with self.lock:
            for key, items in self._catalogues.items():
                self._catalogues[key] = [item for item in items
                    if (item.get("deviceId"), item.get("appId"), item.get("path")) not in removed_keys]
            for job in self._jobs:
                item = job.get("item", {})
                if (item.get("deviceId"), item.get("appId"), item.get("path")) in removed_keys:
                    job["remoteDeleted"] = True
            save_pending = self._save_mutation_result_locked()
        self.emit_state()
        deleted = sum(result["deleted"] for result in results)
        return {"results": results, "deleted": deleted, "failed": len(results) - deleted,
                "stateSavePending": save_pending, "cacheWarning": MUTATION_CACHE_WARNING if save_pending else None}

    def _store_delete_plan(self, kind: str, files: list[dict], **metadata) -> dict:
        token = uuid.uuid4().hex
        with self.lock:
            now = time.monotonic()
            self._delete_plans = {key: plan for key, plan in self._delete_plans.items() if plan["expires"] > now}
            self._delete_plans[token] = {"kind": kind, "files": copy.deepcopy(files), "expires": now + PLAN_LIFETIME, **metadata}
        return {"token": token, "files": [{k: copy.deepcopy(v) for k, v in file.items()
                    if k not in {"signature", "identity", "modifiedNs"}} for file in files],
                "fileCount": len(files), "bytes": sum(file["size"] for file in files), **metadata}

    def _take_delete_plan(self, token: str, kind: str) -> dict:
        with self.lock:
            plan = self._delete_plans.pop(token, None) if isinstance(token, str) else None
        if not plan or plan["kind"] != kind or plan["expires"] <= time.monotonic():
            raise UserError("This deletion confirmation expired or was already used. Review the files again.")
        return plan

    def _reject_busy_remote(self, item: dict) -> None:
        with self.lock:
            for job in self._jobs:
                if job.get("deviceId") != item["deviceId"] or job.get("state") in {"completed", "cancelled", "failed"}:
                    continue
                source = job.get("item", {})
                if (source.get("id") == item["id"] or job.get("appId") == item["appId"]
                        and job.get("remoteDir") == posixpath.dirname(item["path"])):
                    raise UserError("Wait for transfers involving this Device folder to finish before deleting files.")

    def _validate_phone_delete_item(self, afc: Any, item: dict) -> dict:
        if item.get("source") != "app":
            raise UserError("Deleting Photos through USB is unavailable: iOS requires its Photos library API. EyeCopy can safely delete files in shared app documents.")
        self._require_app(item["deviceId"], item.get("appId"))
        path = remote_path(item["path"], "app")
        if path == "/Documents":
            raise UserError("Open a folder and select ordinary files to delete. Folder deletion is unavailable.")
        self._check_directory(afc, posixpath.dirname(path), "app")
        stat = afc.stat(path)
        if stat.get("st_ifmt") != "S_IFREG" or item.get("kind") == "folder":
            raise UserError("Only ordinary files in shared app documents can be deleted. Folders and symbolic links are protected.")
        self._reject_busy_remote(item)
        return signature(stat)

    def preview_phone_delete(self, supplied_items: list[dict]) -> dict:
        items = self.resolve_transfer_items(supplied_items)["items"]
        files = []
        for item in items:
            if item.get("source") != "app":
                raise UserError("Deleting Photos through USB is unavailable: iOS requires its Photos library API. EyeCopy can safely delete files in shared app documents.")
            with self._afc(item["deviceId"], "app", item.get("appId")) as afc:
                current = self._validate_phone_delete_item(afc, item)
            files.append({"id": item["id"], "name": item["name"], "path": item["path"], "source": "app",
                          "appId": item["appId"], "deviceId": item["deviceId"], "kind": item["kind"],
                          "size": current["size"], "signature": current})
        return self._store_delete_plan("phone", files, permanent=True,
            warning="Permanently delete these files from the Device? USB deletion cannot use Recently Deleted. Copies on your PC will be kept.")

    def confirm_phone_delete(self, token: str) -> dict:
        plan = self._take_delete_plan(token, "phone")
        with self.lock:
            # An older in-flight scan must not restore a deleted entry afterward.
            self._scan_generation += 1
            self._scan_request = None
            self.scanning = False
        results = []
        # Validate every source before any deletion, so changed selections fail
        # without partially removing a mixed batch.
        for item in plan["files"]:
            current = self._resolve_item({"id": item["id"]}, item["deviceId"])
            if any(current.get(key) != item.get(key) for key in ("path", "source", "appId")):
                raise UserError("The selected Device files changed. Review the deletion again.")
            with self._afc(item["deviceId"], "app", item["appId"]) as afc:
                if self._validate_phone_delete_item(afc, item) != item["signature"]:
                    raise SourceChangedError("A Device file changed after the warning appeared. Nothing was deleted; review the files again.")
        deleted_ids = set()
        for item in plan["files"]:
            result = {"id": item["id"], "name": item["name"], "path": item["path"], "deleted": False}
            try:
                with self._afc(item["deviceId"], "app", item["appId"]) as afc:
                    if self._validate_phone_delete_item(afc, item) != item["signature"]:
                        raise SourceChangedError("This Device file changed and was kept. Review it again.")
                    afc.rm_single(item["path"])
                    if afc.exists(item["path"]):
                        raise UserError("The Device did not confirm deletion. Refresh and check this file.")
                result["deleted"] = True
                deleted_ids.add(item["id"])
            except Exception as error:
                result["error"] = public_error(error)
            results.append(result)
        with self.lock:
            for key, items in self._catalogues.items():
                self._catalogues[key] = [item for item in items if item.get("id") not in deleted_ids]
            for job in self._jobs:
                if job.get("item", {}).get("id") in deleted_ids:
                    job["remoteDeleted"] = True
            save_pending = self._save_mutation_result_locked()
        self.emit_state()
        return {"results": results, "deleted": len(deleted_ids), "failed": len(results) - len(deleted_ids),
                "stateSavePending": save_pending, "cacheWarning": MUTATION_CACHE_WARNING if save_pending else None}

    @staticmethod
    def _ordinary_pc_file(path: Path, root: Path) -> bool:
        # Do not resolve first: doing so would hide reparse/symlink traversal.
        try:
            if not path.is_absolute() or path.parent != root or path.name in {"", ".", ".."}:
                return False
            for component in [path, root, *root.parents]:
                stat = component.lstat()
                if component.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                    return False
            return path.is_file()
        except (OSError, ValueError):
            return False

    def _owned_pc_record(self, record: dict, root: Path, device_id: str) -> bool:
        try:
            path = Path(record["path"])
            return (record.get("deviceId") == device_id and self._ordinary_pc_file(path, root)
                    and self._file_identity(path) == record["identity"]
                    and path.stat().st_mtime_ns == record["modifiedNs"])
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def preview_pc_cleanup(self, device_id: str) -> dict:
        with self.lock:
            self._ensure_device_directories_locked()
            root = Path(self._devices[device_id]["defaultDownloadDir"])
            records = copy.deepcopy(list(self._download_records.values()))
        files = [{**record, "size": record["identity"]["size"], "name": Path(record["path"]).name}
                 for record in records if self._owned_pc_record(record, root, device_id)]
        return self._store_delete_plan("pc", files, deviceId=device_id, path=str(root), permanent=os.name != "nt",
            warning=("Move EyeCopy's downloaded copies from this Device's default PC folder to the Recycle Bin? Device originals, custom destinations, edited copies, and other files are kept."
                     if os.name == "nt" else "Permanently remove EyeCopy's downloaded copies from this Device's default PC folder? Device originals and other files are kept."))

    def preview_pc_delete(self, device_id: str, supplied_items: list[dict]) -> dict:
        """Preview only selected, unchanged originals in the current PC library."""
        items = self.resolve_transfer_items(supplied_items, device_id)["items"]
        selected = {item["id"]: item for item in items}
        with self.lock:
            self._ensure_device_directories_locked()
            root = Path(self._devices[device_id]["defaultDownloadDir"])
            records = copy.deepcopy(list(self._download_records.values()))
        files = [{**record, "size": record["identity"]["size"], "name": Path(record["path"]).name}
                 for record in records if record.get("itemId") in selected
                 and self._record_matches_item(record, selected[record["itemId"]])
                 and self._owned_pc_record(record, root, device_id)]
        warning = ("Move these selected downloaded copies to the Recycle Bin? " if os.name == "nt" else
                   "Permanently remove these selected downloaded copies? ")
        return self._store_delete_plan("pc-selection", files, deviceId=device_id, path=str(root),
            permanent=os.name != "nt", historyLabel="Delete selected PC downloads",
            warning=warning + "Device originals, unselected PC files, edited copies, and files outside the default download folder are kept.")

    @staticmethod
    def _remove_pc_copy(path: Path, validate=lambda: True) -> dict | None:
        if os.name != "nt":
            path.unlink()
            return
        from backend.pc_recycle import recycle_file
        try:
            return recycle_file(path, validate)
        except Exception as error:
            raise UserError("Windows could not confirm moving this file to the Recycle Bin. No permanent deletion was attempted; refresh and check the folder.") from error

    def confirm_pc_cleanup(self, token: str, *, kind: str = "pc") -> dict:
        plan = self._take_delete_plan(token, kind)
        device_id = self._require_device(plan["deviceId"])
        with self.lock:
            self._ensure_device_directories_locked()
            root = Path(self._devices[device_id]["defaultDownloadDir"])
        if str(root) != plan["path"]:
            raise UserError("The Device's PC folder changed. Review cleanup again.")
        results, deleted_paths, history_info = [], set(), {}
        for record in plan["files"]:
            result = {"name": record["name"], "path": record["path"], "deleted": False}
            try:
                with self.lock:
                    authoritative = self._download_records.get(record["path"])
                    if authoritative != {key: value for key, value in record.items() if key not in {"size", "name"}}:
                        raise UserError("This file's download record changed. It was kept.")
                if not self._owned_pc_record(record, root, device_id):
                    raise UserError("This PC file changed, moved, or is no longer an original download. It was kept.")
                receipt = self._remove_pc_copy(Path(record["path"]), lambda: self._owned_pc_record(record, root, device_id))
                result["deleted"] = True
                deleted_paths.add(record["path"])
                if isinstance(receipt, dict):
                    if receipt.get("undoUnavailable"):
                        result["receiptWarning"] = "Windows recycled this copy but could not identify its exact recovery entry. Restore it manually from Recycle Bin if needed."
                        results.append(result)
                        continue
                    receipt["originalRecord"] = copy.deepcopy(authoritative)
                    if history_info.get("undoToken"):
                        history_info = self._pc_history.append(history_info["undoToken"], [receipt])
                    else:
                        history_info = self._pc_history.record([receipt],
                            plan.get("historyLabel") or f"{self._devices[device_id].get('alias') or self._devices[device_id].get('name') or 'Device'} PC cleanup")
            except Exception as error:
                result["error"] = public_error(error)
            results.append(result)
        with self.lock:
            for path in deleted_paths:
                self._download_records.pop(path, None)
            for items in self._catalogues.values():
                for item in self._catalogue_components(items):
                    if item.get("localPath") in deleted_paths:
                        item.pop("localPath", None)
            for job in self._jobs:
                if job.get("localPath") in deleted_paths:
                    job.pop("localPath", None)
            save_pending = self._save_mutation_result_locked()
        self.emit_state()
        history_pending = bool(history_info.get("savePending"))
        return {"results": results, "deleted": len(deleted_paths), "failed": len(results) - len(deleted_paths),
                "receiptWarning": next((result["receiptWarning"] for result in results if result.get("receiptWarning")), None),
                "undoToken": history_info.get("undoToken"), "historyStatus": history_info.get("historyStatus"),
                "stateSavePending": save_pending or history_pending,
                "cacheWarning": MUTATION_CACHE_WARNING if save_pending or history_pending else None}

    def confirm_pc_delete(self, token: str) -> dict:
        return self.confirm_pc_cleanup(token, kind="pc-selection")

    def _apply_pc_history_records_locked(self, rows: list[dict]) -> None:
        for row in rows:
            record = row.get("originalRecord")
            try:
                path = Path(row["path"])
                if (not isinstance(record, dict) or record.get("deviceId") not in self._devices
                        or record.get("path") != str(path) or not self._ordinary_pc_file(path, path.parent)):
                    continue
                self._download_records[str(path)] = {**copy.deepcopy(record),
                    "identity": self._file_identity(path), "modifiedNs": path.stat().st_mtime_ns}
            except (OSError, ValueError, TypeError, KeyError):
                continue

    def change_pc_cleanup_history(self, undo: bool, token: str | None = None) -> dict:
        with self.lock:
            known_devices = set(self._devices)
        def permitted(receipt):
            record = receipt.get("originalRecord")
            return (isinstance(record, dict) and record.get("deviceId") in known_devices
                    and record.get("path") == receipt.get("originalPath"))
        try:
            outcome = (self._pc_history.undo if undo else self._pc_history.redo)(token, validate=permitted)
        except (ValueError, OSError) as error:
            raise UserError(str(error)) from error
        with self.lock:
            self._apply_pc_history_records_locked([row for row in outcome["results"] if row.get("restored")])
            for row in outcome["results"]:
                if row.get("deleted"):
                    self._download_records.pop(row["path"], None)
                    for job in self._jobs:
                        if job.get("localPath") == row["path"]:
                            job.pop("localPath", None)
            self._reconcile_default_copies_locked()
            save_pending = self._save_mutation_result_locked()
        # Native Recycle Bin paths and internal ownership identities stay private.
        result = {**outcome, "results": [{key: value for key, value in row.items() if key != "originalRecord"}
                                         for row in outcome["results"]]}
        flag = "restored" if undo else "deleted"
        result["failed"] = sum(not row.get(flag) for row in result["results"])
        result["stateSavePending"] = save_pending or bool(outcome.get("savePending"))
        result["cacheWarning"] = MUTATION_CACHE_WARNING if result["stateSavePending"] else None
        self.emit_state()
        return result

    @staticmethod
    def _check_directory(afc: Any, path: str, source: str) -> None:
        path = remote_path(path, source)
        root = "/DCIM" if source == "photos" else "/Documents"
        segments = path[len(root):].strip("/").split("/") if path != root else []
        current = root
        for segment in [None, *segments]:
            if segment is not None:
                current = posixpath.join(current, segment)
            if afc.stat(current).get("st_ifmt") != "S_IFDIR":
                raise UserError("The Device folder is unavailable or is a symbolic link. Refresh and choose another folder.")

    @staticmethod
    def _leaf(name: str) -> bool:
        return bool(name) and name not in {".", ".."} and not any(c in name for c in ("/", "\\", "\x00"))

    def _catalogue_item(self, device_id: str, source: str, app_id: str | None, path: str, stat: dict) -> dict:
        identifier = hashlib.sha256(json.dumps([device_id, source, app_id, path]).encode("utf-8")).hexdigest()[:24]
        return {"id": identifier, "deviceId": device_id, "source": source, "appId": app_id, "path": path,
                "parentPath": posixpath.dirname(path),
                "name": posixpath.basename(path), "size": int(stat.get("st_size", 0)), "modified": timestamp(stat.get("st_mtime")),
                "kind": classify(path, stat.get("st_ifmt") == "S_IFDIR")}

    @staticmethod
    def _catalogue_components(items):
        for item in items:
            yield item
            live = item.get("livePhoto") if isinstance(item, dict) else None
            if isinstance(live, dict) and live.get("verified") is True and isinstance(live.get("video"), dict):
                yield live["video"]

    def _inspect_live_item(self, afc, item, cached, token):
        if item["source"] != "photos" or Path(item["name"]).suffix.lower() not in LIVE_SUPPORTED_EXTENSIONS:
            return
        old = cached.get("_livePhotoMetadata")
        if item["size"] == cached.get("size") and item["modified"] == cached.get("modified") and self._current_live_metadata(cached):
            item["_livePhotoMetadata"] = copy.deepcopy(old)
            return
        handle = None
        try:
            handle = afc.fopen(item["path"], "r")
            def read_at(offset, count):
                with self.lock:
                    if token != self._scan_generation or self.stop.is_set():
                        raise TransferInterrupted("cancelled")
                afc_seek(afc, handle, offset)
                return afc_read(afc, handle, count)
            item["_livePhotoMetadata"] = inspect_live_metadata(read_at, item["size"], item["name"])
        except TransferInterrupted:
            raise
        except Exception as error:
            # Missing/unsupported metadata must leave an ordinary file visible.
            LOG.debug("Optional Live Photo metadata unavailable: %s", type(error).__name__)
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    afc.fclose(handle)

    @staticmethod
    def _current_live_metadata(item):
        metadata = item.get("_livePhotoMetadata")
        return isinstance(metadata, dict) and type(metadata.get("version")) is int and metadata["version"] == LIVE_METADATA_VERSION

    def _needs_live_photo_scan(self, items):
        return any(item.get("kind") in {"photo", "video"} and
                   Path(item.get("name", "")).suffix.lower() in LIVE_SUPPORTED_EXTENSIONS and
                   not self._current_live_metadata(item) for item in self._catalogue_components(items))

    @staticmethod
    def _video_cache_signature(item: dict) -> dict:
        return {"size": item.get("size"), "modified": item.get("modified")}

    def _current_video_metadata(self, item: dict) -> bool:
        metadata = item.get("_videoMetadata")
        return (isinstance(metadata, dict) and type(metadata.get("version")) is int and metadata.get("version") == VIDEO_METADATA_VERSION
                and "durationSeconds" in metadata
                and metadata.get("signature") == self._video_cache_signature(item)
                and (metadata.get("durationSeconds") is None or self._valid_duration(metadata.get("durationSeconds"))))

    @staticmethod
    def _valid_duration(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0

    def _apply_video_metadata(self, item: dict, duration: float | None) -> None:
        item["_videoMetadata"] = {"version": VIDEO_METADATA_VERSION, "signature": self._video_cache_signature(item),
                                  "durationSeconds": duration}
        item["durationSeconds"] = duration

    def _inspect_video_item(self, afc, item: dict, cached: dict, token: int | None = None) -> None:
        if item.get("kind") != "video" or Path(item["name"]).suffix.lower() not in DURATION_EXTENSIONS:
            return
        if (self._current_video_metadata(cached)
                and self._video_cache_signature(item) == self._video_cache_signature(cached)):
            self._apply_video_metadata(item, cached["_videoMetadata"]["durationSeconds"])
            return
        # Live Photo inspection has already read a bounded movie header. Reuse
        # its duration when present; the independent parser handles large moov
        # boxes without reading track tables or requiring a playable codec.
        live = item.get("_livePhotoMetadata", {})
        if self._valid_duration(live.get("durationSeconds")):
            self._apply_video_metadata(item, float(live["durationSeconds"]))
            return
        handle = None
        try:
            before = signature(afc.stat(item["path"]))
            if before != self._video_cache_signature(item):
                raise SourceChangedError("This Device video changed. Refresh its catalogue before reading details.")
            handle = afc.fopen(item["path"], "r")
            def read_at(offset, count):
                if token is not None:
                    with self.lock:
                        if token != self._scan_generation or self.stop.is_set():
                            raise TransferInterrupted("cancelled")
                afc_seek(afc, handle, offset)
                return afc_read(afc, handle, count)
            duration = inspect_duration(read_at, item["size"], item["name"])
            if signature(afc.stat(item["path"])) != before:
                raise SourceChangedError("This Device video changed while reading its details. Refresh the catalogue.")
            self._apply_video_metadata(item, duration)
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    afc.fclose(handle)

    def video_metadata(self, supplied: dict) -> dict:
        item = self._resolve_item(supplied)
        if item.get("livePhoto", {}).get("verified") is True:
            item = self._resolve_item({"id": item["livePhoto"]["video"]["id"]})
        if item.get("kind") != "video":
            raise UserError("Choose a video to read its duration.")
        extension = Path(item["name"]).suffix.lower()
        if extension not in DURATION_EXTENSIONS:
            return {"itemId": item["id"], "sourceSignature": self._video_cache_signature(item),
                    "durationSeconds": None, "available": False, "unsupported": True}
        if not self._current_video_metadata(item):
            local = Path(item["localPath"]) if item.get("localPath") else None
            if local is not None:
                with self.lock:
                    record = self._download_records.get(str(local))
                    root = Path(self._devices[item["deviceId"]]["defaultDownloadDir"])
                    if not record or not self._owned_pc_record(record, root, item["deviceId"]):
                        local = None
            if local is not None and local.is_file():
                before = local_signature(local)
                with local.open("rb") as stream:
                    def read_at(offset, count):
                        stream.seek(offset)
                        return stream.read(count)
                    duration = inspect_duration(read_at, before["size"], item["name"])
                if local_signature(local) != before:
                    raise SourceChangedError("This downloaded video changed while reading its details. Refresh the catalogue.")
                self._apply_video_metadata(item, duration)
            elif self._available(item["deviceId"]):
                with self._afc(item["deviceId"], item["source"], item.get("appId")) as afc:
                    self._check_directory(afc, posixpath.dirname(item["path"]), item["source"])
                    self._inspect_video_item(afc, item, item)
            else:
                return {"itemId": item["id"], "sourceSignature": self._video_cache_signature(item),
                        "durationSeconds": None, "available": False, "offline": True}
            with self.lock:
                for items in self._catalogues.values():
                    for cached in self._catalogue_components(items):
                        if cached.get("id") == item["id"] and self._video_cache_signature(cached) == self._video_cache_signature(item):
                            cached["_videoMetadata"] = copy.deepcopy(item["_videoMetadata"])
                            cached["durationSeconds"] = item["durationSeconds"]
                with contextlib.suppress(OSError):
                    self._save_locked()
        duration = item["_videoMetadata"]["durationSeconds"]
        result = {"itemId": item["id"], "sourceSignature": self._video_cache_signature(item),
                  "durationSeconds": duration, "available": self._valid_duration(duration)}
        self._emit("video_metadata", result)
        return result

    def request_scan(self, device_id: str, source: str, app_id: str | None = None, path: str | None = None) -> dict:
        path = remote_path(path or ("/DCIM" if source == "photos" else "/Documents"), source)
        if source == "app":
            self._require_app(device_id, app_id)
        with self.lock:
            self._scan_generation += 1
            token = self._scan_generation
            self.current_scope = self.scope(device_id, source, app_id, path)
            self._scan_request = (token, device_id, source, app_id, path)
            self.scanning = True
            self._save_locked()
        self.emit_state()
        self._emit("scan", {"scanning": True, "count": 0})
        self.scan_wake.set()
        return {"started": True}

    def _scan_loop(self) -> None:
        while not self.stop.is_set():
            self.scan_wake.wait(1)
            self.scan_wake.clear()
            with self.lock:
                request, self._scan_request = self._scan_request, None
            if request is None:
                continue
            self._scan(*request)

    def _scan(self, token: int, device_id: str, source: str, app_id: str | None, path: str) -> None:
        scope = self.scope(device_id, source, app_id, path)
        with self.lock:
            previous = {i["id"]: i for i in self._catalogue_components(self._catalogues.get(scope, []))}
        found: list[dict] = []
        last_emit = time.monotonic()
        error_message = None

        def publish(final: bool = False):
            with self.lock:
                if token != self._scan_generation:
                    return
                merged = {**previous, **{i["id"]: i for i in found}}
                items = found if final else list(merged.values())
                if source == "photos":
                    items = group_live_photos(items)
                self._catalogues[scope] = copy.deepcopy(items)
                self._reconcile_default_copies_locked()
                items = copy.deepcopy(self._catalogues[scope])
            self._emit("catalogue", {"deviceId": device_id, "items": copy.deepcopy(items), "source": source, "appId": app_id, "path": path})
            self._emit("scan", {"scanning": not final, "count": len(found)})

        try:
            with self._afc(device_id, source, app_id) as afc:
                self._check_directory(afc, path, source)
                stack = [path]
                visited: set[str] = set()
                while stack:
                    with self.lock:
                        if token != self._scan_generation or self.stop.is_set():
                            return
                    directory = stack.pop()
                    if directory in visited:
                        continue
                    visited.add(directory)
                    for name in afc.listdir(directory):
                        if not self._leaf(name):
                            continue
                        if source == "app" and re.fullmatch(r"\.eyecopy-[0-9a-f]{32}\.part", name):
                            # Owned upload recovery artifacts are not user files.
                            continue
                        full_path = remote_path(posixpath.join(directory, name), source)
                        metadata = afc.stat(full_path)
                        file_type = metadata.get("st_ifmt")
                        if file_type == "S_IFDIR" and source == "photos":
                            stack.append(full_path)
                            continue
                        if file_type not in {"S_IFDIR", "S_IFREG"}:
                            continue
                        item = self._catalogue_item(device_id, source, app_id, full_path, metadata)
                        cached = previous.get(item["id"], {})
                        if cached.get("modified") == item["modified"] and cached.get("size") == item["size"] and cached.get("localPath"):
                            item["localPath"] = cached["localPath"]
                        if file_type == "S_IFREG":
                            self._inspect_live_item(afc, item, cached, token)
                            try:
                                self._inspect_video_item(afc, item, cached, token)
                            except TransferInterrupted:
                                raise
                            except Exception as error:
                                LOG.debug("Optional video duration unavailable: %s", type(error).__name__)
                        found.append(item)
                        if len(found) % 200 == 0 or time.monotonic() - last_emit > 1.5:
                            publish()
                            last_emit = time.monotonic()
                publish(final=True)
        except Exception as error:
            error_message = public_error(error)
            publish()
        finally:
            with self.lock:
                if token == self._scan_generation:
                    self.scanning = False
                self._save_locked()
            self._emit("scan", {"scanning": self.scanning, "count": len(found), **({"error": error_message} if error_message else {})})

    def _catalogue_index_locked(self) -> dict:
        catalogues = [self._catalogues.get(self.current_scope or "", [])]
        catalogues.extend(items for key, items in self._catalogues.items() if key != self.current_scope)
        index = {}
        for items in catalogues:
            for cached in self._catalogue_components(items):
                index.setdefault(cached.get("id"), cached)
        return index

    def _resolve_catalogue_items(self, supplied_items: list[dict], device_id: str | None = None) -> list[dict]:
        if not isinstance(supplied_items, list) or not 0 < len(supplied_items) <= 20000:
            raise UserError("Select one or more files to transfer.")
        identifiers = []
        for supplied in supplied_items:
            if not isinstance(supplied, dict) or not isinstance(supplied.get("id"), str) or not supplied["id"]:
                raise UserError("Choose a file from the Device catalogue.")
            identifiers.append(supplied["id"])
        with self.lock:
            self._ensure_device_directories_locked()
            index = self._catalogue_index_locked()
            snapshots = []
            for identifier in identifiers:
                item = index.get(identifier)
                if item is None or device_id and item.get("deviceId") != device_id:
                    raise UserError("Refresh the catalogue, then select that file again.")
                snapshots.append(copy.deepcopy(item))
            components = {item["id"]: item for item in self._catalogue_components(snapshots)}
            candidates = copy.deepcopy([record for record in self._download_records.values()
                                        if record.get("itemId") in components])
            devices = copy.deepcopy(self._devices)

        # Potentially slow disk metadata belongs outside the state lock: Stop
        # All must still respond while an unavailable destination is inspected.
        verified = self._validated_pc_records(candidates, devices)
        with self.lock:
            current_index = self._catalogue_index_locked()
            relevant_fields = ("deviceId", "source", "appId", "path", "size", "modified", "kind")
            for identifier, snapshot in components.items():
                current = current_index.get(identifier)
                if current is None or any(current.get(field) != snapshot.get(field) for field in relevant_fields):
                    raise UserError("The selected files changed. Refresh the catalogue and select them again.")
            local_paths = {}
            for identifier, item in components.items():
                root = self._devices.get(item["deviceId"], {}).get("defaultDownloadDir")
                matching = next((record for record in verified.get(identifier, [])
                                 if Path(record["path"]).parent == Path(root or "")
                                 and self._download_records.get(record["path"]) == record
                                 and self._record_matches_item(record, item)), None)
                local_paths[identifier] = matching["path"] if matching and item.get("kind") != "folder" else None
            for item in self._catalogue_components(snapshots):
                local = local_paths[item["id"]]
                item.pop("localPath", None)
                if local:
                    item["localPath"] = local
            changed = False
            for items in self._catalogues.values():
                for cached in self._catalogue_components(items):
                    identifier = cached.get("id")
                    if identifier not in local_paths:
                        continue
                    local = local_paths[identifier]
                    if cached.get("localPath") != local:
                        cached.pop("localPath", None)
                        if local:
                            cached["localPath"] = local
                        changed = True
            if changed:
                self._save_locked()
        return snapshots

    def _resolve_item(self, supplied: dict, device_id: str | None = None) -> dict:
        return self._resolve_catalogue_items([supplied], device_id)[0]

    def resolve_transfer_items(self, supplied_items: list[dict], device_id: str | None = None) -> dict:
        result, seen = [], set()
        for item in self._resolve_catalogue_items(supplied_items, device_id):
            components = [item]
            live = item.get("livePhoto")
            if isinstance(live, dict) and live.get("verified") is True:
                video = live.get("video")
                if not isinstance(video, dict) or video.get("deviceId") != item["deviceId"] or video.get("source") != "photos" or video.get("kind") != "video":
                    raise UserError("Refresh this Live Photo before transferring it.")
                components.append(copy.deepcopy(video))
            for component in components:
                if component["kind"] == "folder":
                    raise UserError("Open the folder and select files inside it to transfer.")
                remote_path(component["path"], component["source"])
                if component["id"] not in seen:
                    seen.add(component["id"])
                    component.pop("livePhoto", None)
                    result.append(component)
        with self.lock:
            offline = sorted({item["deviceId"] for item in result if not self._devices.get(item["deviceId"], {}).get("online")})
        # Preparing a drag never replaces the renderer's active gesture DOM.
        return {"items": result, "offlineDeviceIds": offline}

    def queue_generation(self) -> int:
        with self._queue_generation_lock:
            return self._queue_generation

    def _invalidate_queue_batches(self) -> None:
        with self._queue_generation_lock:
            self._queue_generation += 1

    def _new_job(self, name: str, direction: str, device_id: str, total: int, **internal) -> dict:
        return {"id": uuid.uuid4().hex, "name": name, "direction": direction, "deviceId": device_id,
                "state": "queued", "bytes": 0, "total": total, "speed": 0, "eta": None, "error": None, **internal}

    def enqueue_download(self, device_id: str, items: list[dict], destination: str | None = None, *, defer: bool = False,
                         sync: bool | None = None, enqueue_generation: int | None = None) -> dict:
        if not items or len(items) > 20000:
            raise UserError("Select one or more files to download.")
        with self.lock:
            generation = self.queue_generation() if enqueue_generation is None else enqueue_generation
            if generation != self.queue_generation():
                return {"jobIds": [], "skippedItemIds": [], "inProgressJobIds": [],
                        "skipped": 0, "queued": 0, "cancelled": True}
            default = self._effective_download_dir_locked(device_id)
            permanent = Path(self._devices[device_id]["defaultDownloadDir"])
        directory = Path(destination or default).expanduser().resolve()
        sync = bool((destination is None and not defer) if sync is None else sync) and directory == permanent
        jobs, skipped, in_progress = [], [], []
        resolved = self.resolve_transfer_items(items, device_id)["items"]
        with self.lock:
            if generation != self.queue_generation():
                return {"jobIds": [], "skippedItemIds": [], "inProgressJobIds": [],
                        "skipped": 0, "queued": 0, "cancelled": True}
            pending_by_item = {job.get("item", {}).get("id"): job for job in self._jobs
                               if sync and job.get("direction") == "download" and job.get("destination") == str(directory)
                               and job.get("state") not in {"completed", "cancelled", "failed"}}
            for item in resolved:
                if item["kind"] == "folder":
                    raise UserError("Open the folder and select files inside it to download.")
                remote_path(item["path"], item["source"])
                if sync and item.get("localPath"):
                    skipped.append(item["id"])
                    continue
                pending = pending_by_item.get(item["id"])
                if pending:
                    in_progress.append(pending["id"])
                    continue
                job = self._new_job(item["name"], "download", device_id, item["size"], item=item,
                                    destination=str(directory), syncDownload=sync)
                jobs.append(job)
                if sync:
                    pending_by_item[item["id"]] = job
            if defer:
                # Deferred drops never open a source service or partial output.
                for job in jobs:
                    job.update(state="paused", awaitingStart=True,
                               message="Destination selected. Start transfer when ready.")
            self._jobs.extend(jobs)
        self._publish_jobs(True)
        self.wake.set()
        return {"jobIds": [j["id"] for j in jobs], "skippedItemIds": skipped, "inProgressJobIds": in_progress,
                "skipped": len(skipped), "queued": len(jobs)}

    def enqueue_upload(self, device_id: str, app_id: str, path: str, local_paths: list[str], *,
                       enqueue_generation: int | None = None) -> dict:
        with self.lock:
            generation = self.queue_generation() if enqueue_generation is None else enqueue_generation
            if generation != self.queue_generation():
                return {"jobIds": [], "queued": 0, "cancelled": True}
        self._require_app(device_id, app_id)
        path = remote_path(path, "app")
        if not local_paths or len(local_paths) > 20000:
            raise UserError("Choose files on your PC to upload.")
        jobs = []
        for local in local_paths:
            source = Path(local).expanduser().resolve()
            if not source.is_file():
                raise UserError("Choose ordinary files to upload. Folder uploads are not supported yet.")
            jobs.append(self._new_job(safe_filename(source.name), "upload", device_id, source.stat().st_size,
                                      appId=app_id, remoteDir=path, sourcePath=str(source)))
        with self.lock:
            if generation != self.queue_generation():
                return {"jobIds": [], "queued": 0, "cancelled": True}
            self._jobs.extend(jobs)
        self._publish_jobs(True)
        self.wake.set()
        return {"jobIds": [j["id"] for j in jobs]}

    def start_queued_job(self, identifier: str) -> dict:
        """Start a newly deferred drop once, without retrying cancelled jobs."""
        with self.lock:
            job = next((j for j in self._jobs if j["id"] == identifier), None)
            if job is None:
                raise UserError("That transfer is no longer in the queue.")
            if (job.get("awaitingStart") is not True or job["state"] != "paused"
                    or job["direction"] != "download" or self._active_job == identifier
                    or self._requests.get(identifier) == "cancelled"):
                return {"changed": False}
            self._requests.pop(identifier, None)
            job.pop("retryAfter", None)
            job.pop("message", None)
            job.update(state="queued", awaitingStart=False, error=None, speed=0, eta=None)
            self._save_locked()
        self._publish_jobs(True)
        self.wake.set()
        return {"changed": True}

    def clear_finished_jobs(self) -> dict:
        """Clear terminal history without deleting files or cleanup records."""
        with self.lock:
            removed = {job["id"] for job in self._jobs
                       if job["state"] in {"completed", "cancelled", "failed"}
                       and job["id"] != self._active_job
                       and not job.get("remoteCleanupPending")
                       and not self._has_owned_local_part(job)}
            self._jobs = [job for job in self._jobs if job["id"] not in removed]
            for identifier in removed:
                self._requests.pop(identifier, None)
            self._save_locked()
        self._publish_jobs(True)
        return {"jobs": self.public_jobs(), "cleared": len(removed)}

    def _cancel_job_locked(self, job: dict) -> bool:
        if job["state"] == "completed":
            return False
        identifier = job["id"]
        changed = self._requests.get(identifier) != "cancelled"
        self._requests[identifier] = "cancelled"
        job["awaitingStart"] = False
        if self._active_job == identifier:
            job.update(cancelRequested=True, message="Stopping transfer…", speed=0, eta=None)
        else:
            job.update(state="cancelled", speed=0, eta=None)
            job.pop("cancelRequested", None)
            job.pop("message", None)
            self._remove_local_part(job)
        if job["direction"] == "upload" and job.get("remoteTemp"):
            job["remoteCleanupPending"] = True
        return changed

    def cancel_all_jobs(self) -> dict:
        """Signal every pending transfer in one commit, without waiting on USB."""
        self._invalidate_queue_batches()
        with self.lock:
            count = sum(self._cancel_job_locked(job) for job in self._jobs
                        if job["state"] not in {"completed", "cancelled", "failed"})
        self._publish_jobs(True)
        self.wake.set()
        return {"changed": bool(count), "count": count, "jobs": self.public_jobs()}

    def clear_all_jobs(self) -> dict:
        """Discard queue listings, retaining executing work and cleanup receipts."""
        blocked, removed = [], set()
        self._invalidate_queue_batches()
        with self.lock:
            for job in self._jobs:
                identifier = job["id"]
                if identifier == self._active_job:
                    self._cancel_job_locked(job)
                    blocked.append({"id": identifier, "reason": "This transfer is still stopping. Clear again after it stops."})
                    continue
                if job["state"] != "completed":
                    # Cancellation removes only the exact nonce-owned partial.
                    self._cancel_job_locked(job)
                    safe_part = self._remove_local_part(job)
                else:
                    safe_part = True
                if job.get("remoteCleanupPending") or not safe_part or self._has_owned_local_part(job):
                    reason = ("Temporary upload cleanup is pending. Reconnect the Device before clearing this entry."
                              if job.get("remoteCleanupPending") else
                              "This temporary download is locked or its ownership could not be confirmed. Its queue entry was kept; inspect the destination before clearing again.")
                    job["error"] = reason
                    blocked.append({"id": identifier, "reason": reason})
                    continue
                removed.add(identifier)
            self._jobs = [job for job in self._jobs if job["id"] not in removed]
            for identifier in removed:
                self._requests.pop(identifier, None)
        self._publish_jobs(True)
        self.wake.set()
        return {"jobs": self.public_jobs(), "cleared": len(removed),
                "retained": len(blocked), "blocked": blocked}

    def control_job(self, identifier: str, action: str) -> dict:
        with self.lock:
            job = next((j for j in self._jobs if j["id"] == identifier), None)
            if job is None:
                raise UserError("That transfer is no longer in the queue.")
            if job["state"] == "completed":
                return {"changed": False}
            if action in {"retry", "resume"}:
                if self._active_job == identifier:
                    raise UserError("Wait for temporary-file cleanup to finish before retrying." if job.get("remoteCleanupPending") else "Pause the active transfer before retrying it.")
                self._requests.pop(identifier, None)
                job.pop("remoteCleanupPending", None)
                job.pop("cleanupRetryAfter", None)
                job.pop("retryAfter", None)
                job.pop("message", None)
                job["awaitingStart"] = False
                job.update(state="queued", error=None, speed=0, eta=None)
            else:
                state = "paused" if action == "pause" else "cancelled"
                if state == "cancelled":
                    self._cancel_job_locked(job)
                elif self._requests.get(identifier) != "cancelled":
                    self._requests[identifier] = state
                    if self._active_job != identifier:
                        job.update(state=state, speed=0, eta=None)
            self._save_locked()
        self._publish_jobs(True)
        self.wake.set()
        return {"changed": True}

    @staticmethod
    def _file_identity(path: Path) -> dict:
        stat = path.stat()
        return {"size": stat.st_size, "inode": stat.st_ino, "device": stat.st_dev}

    @classmethod
    def _published_download_exists(cls, job: dict) -> bool:
        """Recognize only this job's persisted, verified rename source identity."""
        identity = job.get("publicationIdentity")
        if job.get("direction") != "download" or not job.get("publicationPending") or not isinstance(identity, dict) or not identity.get("inode"):
            return False
        try:
            identifier = job.get("id", "")
            if not re.fullmatch(r"[0-9a-f]{32}", identifier):
                return False
            root = Path(job["destination"]).resolve()
            part, final = Path(job["partPath"]).resolve(), Path(job["finalPath"]).resolve()
            return (part.parent == root and final.parent == root
                    and part.name.endswith(owned_part_suffixes(identifier))
                    and not part.exists() and final.is_file() and cls._file_identity(final) == identity)
        except (KeyError, OSError, TypeError, ValueError):
            return False

    @classmethod
    def _recoverable_failed_publication(cls, job: dict) -> bool:
        if (job.get("state") != "failed" or job.get("publicationRecoveryAttempted")
                or not (job.get("error") == LEGACY_FOLDER_DENIED or job.get("publicationPending"))
                or not cls._has_owned_local_part(job)):
            return False
        source = job.get("sourceSignature")
        try:
            size = source["size"]
            return size > 0 and job.get("total") == size and Path(job["partPath"]).stat().st_size == size
        except (KeyError, OSError, TypeError):
            return False

    def _record_local_download(self, job: dict, final: Path) -> None:
        with self.lock:
            job["localPath"] = str(final)
            job["finalPath"] = str(final)
            self._ensure_device_directories_locked()
            if final.parent == Path(self._devices[job["deviceId"]]["defaultDownloadDir"]):
                self._download_records[str(final)] = {
                    "deviceId": job["deviceId"], "itemId": job["item"]["id"], "path": str(final),
                    "identity": self._file_identity(final), "modifiedNs": final.stat().st_mtime_ns,
                    "sourceSignature": copy.deepcopy(job.get("sourceSignature") or {
                        "size": job["item"]["size"], "modified": job["item"].get("modified")}),
                }
            self._reconcile_default_copies_locked()

    @staticmethod
    def _has_owned_local_part(job: dict) -> bool:
        if job.get("direction") != "download" or job.get("state") == "completed":
            return False
        path, destination, identifier = job.get("partPath"), job.get("destination"), job.get("id", "")
        if not path or not destination or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            return False
        try:
            target, root = Path(path).resolve(), Path(destination).resolve()
            return target.parent == root and target.name.endswith(owned_part_suffixes(identifier)) and target.is_file()
        except (ValueError, TypeError):
            return False
        except OSError:
            # Unavailable metadata must not discard the recovery record.
            return True

    @classmethod
    def _bounded_job_history(cls, jobs: list[dict]) -> list[dict]:
        cutoff = max(0, len(jobs) - 500)
        return [job for index, job in enumerate(jobs)
                if index >= cutoff or job.get("state") not in {"completed", "cancelled", "failed"}
                or job.get("remoteCleanupPending") or cls._has_owned_local_part(job)]

    @classmethod
    def _remove_local_part(cls, job: dict) -> bool:
        path = job.get("partPath")
        destination = job.get("destination")
        if not path:
            return True
        identifier = job.get("id", "")
        if not destination or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            return False
        try:
            target, root = Path(path), Path(destination)
            if (not target.is_absolute() or not root.is_absolute() or target.parent != root
                    or not target.name.endswith(owned_part_suffixes(identifier))):
                return False
            if not os.path.lexists(target):
                return True
            metadata = target.lstat()
            if (not cls._plain_directory_path(root) or target.is_symlink()
                    or not file_stat.S_ISREG(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & 0x400):
                return False
            target.unlink()
            return True
        except (OSError, ValueError, TypeError):
            return False

    def _check_control(self, job: dict) -> None:
        with self.lock:
            state = self._requests.get(job["id"])
        if state:
            raise TransferInterrupted(state)
        if self.stop.is_set():
            raise TransferInterrupted("waiting")

    def _progress(self, job: dict, done: int, start: float, initial: int) -> None:
        elapsed = max(time.monotonic() - start, 0.001)
        rate = (done - initial) / elapsed
        with self.lock:
            job.update(bytes=done, speed=rate, eta=(job["total"] - done) / rate if rate > 0 else None)
        self._publish_jobs()

    def _next_work(self) -> tuple[str, dict] | None:
        with self.lock:
            cleanup = next((j for j in self._jobs if j["state"] == "cancelled" and j.get("remoteCleanupPending")
                            and self._available(j["deviceId"]) and j.get("cleanupRetryAfter", 0) <= time.monotonic()), None)
            if cleanup:
                return "cleanup", cleanup
            job = next((j for j in self._jobs if not j.get("awaitingStart") and j["state"] in {"queued", "waiting"} and self._available(j["deviceId"])
                        and (j["state"] == "queued" or j.get("retryAfter", 0) <= time.monotonic())), None)
            return ("transfer", job) if job else None

    def _transfer_loop(self) -> None:
        while not self.stop.is_set():
            with self.lock:
                work = self._next_work()
                if work:
                    operation, job = work
                    self._active_job = job["id"]
            if work is None:
                self.wake.wait(1)
                self.wake.clear()
                continue
            if operation == "cleanup":
                self._cleanup_upload(job)
            else:
                self.run_job(job)
            with self.lock:
                self._active_job = None

    def _cleanup_upload(self, job: dict) -> None:
        """Remove only this cancelled job's owned temporary app document."""
        try:
            directory = remote_path(job.get("remoteDir"), "app")
            identifier = job.get("id", "")
            temporary = job.get("remoteTemp")
            if not re.fullmatch(r"[0-9a-f]{32}", identifier) or not owned_upload_temp(temporary, directory, identifier):
                raise UserError("The temporary-upload path is invalid. No Device files were removed.")
            with self._afc(job["deviceId"], "app", job["appId"]) as afc:
                self._check_directory(afc, directory, "app")
                if afc.exists(temporary):
                    if afc.stat(temporary).get("st_ifmt") != "S_IFREG":
                        raise UserError("The temporary-upload path is not an ordinary file. No Device files were removed.")
                    afc.rm_single(temporary)
            with self.lock:
                job.pop("remoteCleanupPending", None)
                job.pop("cleanupRetryAfter", None)
                job["error"] = None
        except Exception as error:
            with self.lock:
                job["remoteCleanupPending"] = True
                job["cleanupRetryAfter"] = time.monotonic() + 6
                job["error"] = "Temporary upload cleanup is pending. " + public_error(error)
            LOG.warning("Temporary upload cleanup: %s", type(error).__name__)
        finally:
            self._publish_jobs(True)

    def run_job(self, job: dict) -> None:
        # Guard callers in addition to the worker selector. A recorded drop must
        # never turn into a phone read just because another path calls run_job.
        with self.lock:
            if job.get("awaitingStart"):
                return
        try:
            self._check_control(job)
            with self.lock:
                job.update(state="transferring", error=None, speed=0, eta=None)
                job.pop("failureDetails", None)
                job.pop("message", None)
            self._publish_jobs(True)
            if job["direction"] == "download":
                self._transfer_download(job)
            else:
                self._transfer_upload(job)
            with self.lock:
                job.update(state="completed", bytes=job["total"], eta=0, error=None)
                if job["direction"] == "download":
                    job.pop("message", None)
                    job.pop("publicationPending", None)
                job.pop("remoteCleanupPending", None)
                job.pop("cleanupRetryAfter", None)
            if job["direction"] == "download" and job.get("localPath"):
                # Only the owned, committed output is announced. Explorer can
                # otherwise retain deleted tickets or miss newly written media.
                with contextlib.suppress(Exception):
                    notify_local_download(job["localPath"])
        except TransferInterrupted as error:
            with self.lock:
                job.update(state=error.state, speed=0, eta=None)
                job.pop("message", None)
            if error.state == "cancelled":
                self._remove_local_part(job)
        except Exception as error:
            kind = type(error).__name__
            status = getattr(error, "status", None)
            status_code = getattr(status, "value", status)
            disconnected = isinstance(error, (ConnectionError, TimeoutError, socket.timeout, BrokenPipeError)) or any(x in kind for x in ("Connection", "NoDevice", "DeviceNotFound", "Mux")) or status_code in {11, 12, 30}
            with self.lock:
                requested = self._requests.get(job["id"])
                message = public_error(error)
                if job.get("downloadStage") == "state-save" and isinstance(error, OSError):
                    message = "Windows blocked saving transfer recovery information. The downloaded copy is preserved. Close any app using EyeCopy's state file, then retry."
                job.update(state=requested or ("waiting" if disconnected else "failed"), speed=0, eta=None,
                           error=None if requested else message)
                job.pop("message", None)
                cause = error
                while isinstance(cause.__cause__, Exception):
                    cause = cause.__cause__
                job["failureDetails"] = {"stage": job.get("downloadStage") if job["direction"] == "download" else "upload",
                                         "type": type(cause).__name__, "errno": getattr(cause, "errno", None),
                                         "winerror": getattr(cause, "winerror", None)}
                if disconnected and not requested:
                    # A failed transport stays offline until the monitor validates a
                    # new session. Prevent busy retry loops while the cable is flaky.
                    if job["deviceId"] in self._devices:
                        self._devices[job["deviceId"]]["online"] = False
                    self._queried.pop(job["deviceId"], None)
                    job["retryAfter"] = time.monotonic() + 6
                    self.connection.update(status="offline", message="Transfer paused after the USB connection stopped responding. Reconnect the Device to resume.")
            if requested == "cancelled":
                self._remove_local_part(job)
            LOG.warning("Transfer stopped: job=%s %s stage=%s errno=%s winerror=%s", job["id"], kind,
                        job["failureDetails"]["stage"], job["failureDetails"]["errno"], job["failureDetails"]["winerror"])
        finally:
            with self.lock:
                job.pop("cancelRequested", None)
                if self._active_job == job["id"]:
                    self._active_job = None
            self._publish_jobs(True)
            if job["state"] in {"completed", "waiting"}:
                self.emit_state()

    def _publish_download(self, job: dict, part: Path, final: Path, directory: Path) -> Path:
        """Retry short Windows file locks without recopying or overwriting data."""
        retries = 0
        # This durable identity lets restart recognize a successful rename even
        # when a later terminal-history save is blocked by a Windows reader.
        with self.lock:
            job.update(downloadStage="state-save", publicationPending=True,
                       publicationIdentity=self._file_identity(part))
            self._save_locked()
            job["downloadStage"] = "publish"
        while True:
            self._check_control(job)
            try:
                commit_no_overwrite(part, final)
                return final
            except OSError as error:
                # Windows can report ACCESS_DENIED for a directory collision.
                # A new name preserves any target created during the copy.
                if isinstance(error, FileExistsError) or os.name == "nt" and final.exists():
                    final = unique_local_path(directory, job["name"])
                    with self.lock:
                        job.update(finalPath=str(final), downloadStage="state-save")
                        self._save_locked()
                        job["downloadStage"] = "publish"
                    continue
                blocked = getattr(error, "winerror", None) in {5, 32, 33}
                if blocked and retries < len(PUBLISH_RETRY_DELAYS):
                    with self.lock:
                        job.update(message="Waiting for Windows to release this file before finalizing.", speed=0, eta=None)
                    self._publish_jobs(retries == 0)
                    self.stop.wait(PUBLISH_RETRY_DELAYS[retries])
                    retries += 1
                    self._check_control(job)
                    continue
                detail = ("Windows blocked finalizing this file. " if isinstance(error, PermissionError)
                          else "The complete file could not be finalized. ")
                raise UserError(detail + "Its complete .part copy is preserved. Retry this transfer to finish it without downloading the file again.") from error

    def _transfer_download(self, job: dict) -> None:
        item = job["item"]
        path = remote_path(item["path"], item["source"])
        with self.lock:
            self._ensure_device_directories_locked()
            self._reconcile_default_copies_locked()
            if (job.get("syncDownload") and not job.get("partPath")
                    and Path(job["destination"]) == Path(self._devices[job["deviceId"]]["defaultDownloadDir"])):
                matching = next((record for record in self._default_records_by_item_locked().get(item["id"], [])
                                 if self._record_matches_item(record, item)), None)
                if matching:
                    job.update(localPath=matching["path"], finalPath=matching["path"], alreadyDownloaded=True)
                    return
        if self._published_download_exists(job):
            self._record_local_download(job, Path(job["finalPath"]))
            return
        job.pop("publicationPending", None)
        job["downloadStage"] = "destination"
        directory = Path(job["destination"]).resolve()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except PermissionError as error:
            raise UserError("Windows denied creating the download folder. Choose a writable destination.") from error
        job["downloadStage"] = "source"
        with self._afc(job["deviceId"], item["source"], item.get("appId")) as afc:
            self._check_directory(afc, posixpath.dirname(path), item["source"])
            metadata = afc.stat(path)
            if metadata.get("st_ifmt") != "S_IFREG":
                raise UserError("Only ordinary Device files can be downloaded.")
            current_signature = signature(metadata)
            previous_signature = job.get("sourceSignature")
            if previous_signature is not None and previous_signature != current_signature:
                raise SourceChangedError("The Device file changed since this transfer began. Refresh and create a new download; its partial copy was preserved.")
            expected = current_signature["size"]
            job["downloadStage"] = "state-save"
            with self.lock:
                job["sourceSignature"] = current_signature
                job["total"] = expected
                if not job.get("finalPath"):
                    job["finalPath"] = str(unique_local_path(directory, job["name"]))
                    job["partPath"] = job["finalPath"] + f".eyecopy-{job['id']}.part"
                self._save_locked()
            final = Path(job["finalPath"]).resolve()
            part = Path(job["partPath"]).resolve()
            if final.parent != directory or part.parent != directory or not part.name.endswith(owned_part_suffixes(job["id"])):
                raise UserError("The saved transfer destination is invalid. Choose the download again.")
            job["downloadStage"] = "copy"
            offset = part.stat().st_size if part.exists() else 0
            if offset > expected:
                raise SourceChangedError("The partial copy is larger than the Device file. Create a new download.")
            handle = afc.fopen(path, "r") if offset < expected else None
            started = time.monotonic()
            done = offset
            try:
                if handle is not None and offset:
                    afc_seek(afc, handle, offset)
                # A fully received copy may only need publication after a file
                # lock. Do not reopen it for writing or read phone bytes again.
                if handle is not None or not part.exists():
                    with part.open("ab") as output:
                        try:
                            with contextlib.closing(afc_iter_read(afc, handle, expected-done, chunk_size=CHUNK_SIZE,
                                                                 check=lambda: self._check_control(job))) as chunks:
                                for chunk in chunks:
                                    output.write(chunk)
                                    done += len(chunk)
                                    self._progress(job, done, started, offset)
                        except EOFError:
                            raise UserError("The Device returned incomplete file data. The partial copy is preserved; refresh and retry.")
                        output.flush()
                        os.fsync(output.fileno())
                job["downloadStage"] = "verify"
                self._check_control(job)
                if part.stat().st_size != expected or signature(afc.stat(path)) != current_signature:
                    raise SourceChangedError("The Device file changed or was truncated during download. Its partial copy was preserved.")
            finally:
                if handle is not None:
                    with contextlib.suppress(Exception):
                        afc.fclose(handle)
        # Publication and bounded lock retries no longer hold an iPhone socket.
        final = self._publish_download(job, part, final, directory)
        with contextlib.suppress(OSError, ValueError):
            modified = metadata.get("st_mtime")
            if isinstance(modified, datetime):
                os.utime(final, (time.time(), modified.timestamp()))
        self._record_local_download(job, final)

    @staticmethod
    def _unique_remote(afc: Any, directory: str, name: str) -> str:
        name = safe_filename(name)
        base, extension = os.path.splitext(name)
        candidate = posixpath.join(directory, name)
        counter = 2
        while afc.exists(candidate):
            candidate = posixpath.join(directory, f"{base} ({counter}){extension}")
            counter += 1
        return candidate

    def _transfer_upload(self, job: dict) -> None:
        source = Path(job["sourcePath"]).resolve()
        before = local_signature(source)
        if job.get("sourceSignature") not in (None, before):
            raise SourceChangedError("The PC file changed since this upload began. Create a new upload.")
        directory = remote_path(job["remoteDir"], "app")
        with self._afc(job["deviceId"], "app", job["appId"]) as afc:
            self._check_directory(afc, directory, "app")
            with self.lock:
                job["sourceSignature"] = before
                job["total"] = before["size"]
                job.setdefault("remoteTemp", posixpath.join(directory, f".eyecopy-{job['id']}.part"))
                self._save_locked()
            temporary = job["remoteTemp"]
            if not owned_upload_temp(temporary, directory, job["id"]):
                raise UserError("The saved Device transfer path is invalid. Create a new upload.")
            offset = 0
            if afc.exists(temporary):
                metadata = afc.stat(temporary)
                if metadata.get("st_ifmt") != "S_IFREG":
                    raise UserError("The temporary Device path is unavailable. Create a new upload.")
                offset = int(metadata["st_size"])
                if offset > before["size"]:
                    raise UserError("The temporary upload is larger than its source. Create a new upload.")
            handle = afc.fopen(temporary, "r+" if offset else "w")
            done = offset
            started = time.monotonic()
            try:
                if offset:
                    afc_seek(afc, handle, offset)
                with source.open("rb") as input_file:
                    input_file.seek(offset)
                    while done < before["size"]:
                        self._check_control(job)
                        chunk = input_file.read(min(CHUNK_SIZE, before["size"] - done))
                        if not chunk:
                            raise SourceChangedError("The PC file was truncated during upload. Create a new upload.")
                        afc.fwrite(handle, chunk)
                        done += len(chunk)
                        self._progress(job, done, started, offset)
                self._check_control(job)
                if local_signature(source) != before:
                    raise SourceChangedError("The PC file changed during upload. Create a new upload.")
            except TransferInterrupted as error:
                if error.state == "cancelled":
                    job["remoteCleanupPending"] = True
                    with contextlib.suppress(Exception):
                        afc.fclose(handle)
                        handle = None
                        afc.rm_single(temporary)
                        job.pop("remoteCleanupPending", None)
                raise
            finally:
                if handle is not None:
                    with contextlib.suppress(Exception):
                        afc.fclose(handle)
            if int(afc.stat(temporary)["st_size"]) != before["size"]:
                raise UserError("The Device upload size did not match the source. Retry the transfer.")
            final = self._unique_remote(afc, directory, job["name"])
            # Recheck immediately before AFC rename. Existing app documents are
            # never intentionally selected as targets or opened for writing.
            if afc.exists(final):
                final = self._unique_remote(afc, directory, job["name"])
            afc.rename(temporary, final)
            with self.lock:
                job["remotePath"] = final
                job["localPath"] = str(source)

    def _media_server(self):
        with self.lock:
            if self.stop.is_set():
                raise UserError("The media service is restarting. Try again.")
            if self._media is None:
                from backend.streaming import StreamingServer
                self._media = StreamingServer(self)
            return self._media

    def stream(self, supplied: dict, compatible: bool = False, start: float = 0,
               any_file: bool = False, prefer_local: bool = True) -> dict:
        item = self._resolve_item(supplied)
        if item["kind"] == "folder" or (not any_file and item["kind"] != "video"):
            raise UserError("Choose a video to play." if not any_file else "Choose an ordinary file to transfer.")
        local = Path(item["localPath"]) if prefer_local and item.get("localPath") else None
        if local is not None and not local.is_file():
            local = None
        return self._media_server().register(item, compatible=compatible, start=start, local=local,
                                             lifetime=12 * 3600 if any_file else 2 * 3600, allow_empty=any_file,
                                             defer_phone_stat=any_file)

    def preview(self, supplied: dict, compatible: bool = False, start: float = 0) -> dict:
        item = self._resolve_item(supplied)
        local = Path(item["localPath"]) if item.get("localPath") else None
        if compatible and item["kind"] == "video":
            return self.stream(item, compatible=True, start=start)
        if item["kind"] != "photo":
            if local and local.is_file():
                return {"path": str(local.resolve()), "kind": item["kind"], "thumbnail": False}
            raise UserError("Download this file first to preview it. Large videos are never downloaded automatically.")
        identifier = hashlib.sha256(json.dumps([item["id"], item["modified"], item["size"]]).encode()).hexdigest()[:32]
        cache = self.cache_dir / item["deviceId"]
        cache.mkdir(exist_ok=True)
        target = cache / (identifier + ".jpg")
        if target.is_file():
            return {"path": str(target), "kind": "photo", "thumbnail": True}
        from PIL import Image, ImageOps
        import pillow_heif
        pillow_heif.register_heif_opener()
        Image.MAX_IMAGE_PIXELS = 64_000_000
        if local and local.is_file():
            input_image = local
        else:
            if item["size"] > PREVIEW_LIMIT:
                raise UserError("This photo is too large for an automatic preview. Download it first.")
            with self._afc(item["deviceId"], item["source"], item.get("appId")) as afc:
                path = remote_path(item["path"], item["source"])
                self._check_directory(afc, posixpath.dirname(path), item["source"])
                metadata = afc.stat(path)
                if metadata.get("st_ifmt") != "S_IFREG" or int(metadata["st_size"]) > PREVIEW_LIMIT:
                    raise UserError("Download this photo first to preview it.")
                contents = bytearray()
                handle = afc.fopen(path)
                try:
                    size = int(metadata["st_size"])
                    while len(contents) < size:
                        chunk = afc_read(afc, handle, min(CHUNK_SIZE, size - len(contents)))
                        if not chunk:
                            raise UserError("The preview was interrupted. Reconnect and try again.")
                        contents.extend(chunk)
                finally:
                    with contextlib.suppress(Exception):
                        afc.fclose(handle)
            input_image = io.BytesIO(contents)
        with Image.open(input_image) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((1000, 1000))
            image.convert("RGB").save(target, "JPEG", quality=85)
        return {"path": str(target), "kind": "photo", "thumbnail": True}

    def thumbnail(self, supplied: dict) -> dict:
        """Best-effort iOS-generated JPEG; never read the original video.

        PhotoData thumbnail layouts are internal and can change in future iOS
        versions. A missing layout quietly falls back to the file-type icon.
        """
        item = self._resolve_item(supplied)
        unavailable = {"available": False}
        if item.get("kind") not in {"photo", "video"}:
            return unavailable
        try:
            original_path = remote_path(item["path"], item["source"])
        except UserError:
            return unavailable
        identifier = hashlib.sha256(json.dumps([item["id"], item["modified"], item["size"]]).encode()).hexdigest()[:32]
        cache = self.cache_dir / item["deviceId"]
        target = cache / ("thumb-" + identifier + ".jpg")
        # Native thumbnails and photo previews share the same source signature,
        # but have different filenames. Reuse either before consulting the
        # device, decoder availability, or automatic-preview size/work limits.
        cached = (target, cache / (identifier + ".jpg")) if item["kind"] == "photo" else (target,)
        for candidate in cached:
            if candidate.is_file():
                return {"path": str(candidate), "kind": "photo", "thumbnail": True, "available": True}
        # App images and videos have no PhotoData thumbnail. Keep automatic
        # previews bounded and isolate their work from the transfer queue.
        if item.get("source") == "app":
            return self._app_thumbnail(item)
        if not self._available(item["deviceId"]):
            return unavailable
        try:
            from PIL import Image, ImageOps
            with self._afc(item["deviceId"], "photos") as afc:
                for suffix in ("5005.JPG", "5003.JPG"):
                    # The user never supplies a PhotoData path. Only these two
                    # fixed thumbnail names may be requested for a cached DCIM
                    # item whose authoritative path passed root validation.
                    thumbnail_path = "/PhotoData/Thumbnails/V2" + original_path + "/" + suffix
                    try:
                        metadata = afc.stat(thumbnail_path)
                    except Exception:
                        continue
                    size = int(metadata.get("st_size", 0))
                    if metadata.get("st_ifmt") != "S_IFREG" or not 0 < size <= THUMBNAIL_LIMIT:
                        continue
                    handle = afc.fopen(thumbnail_path, "r")
                    contents = bytearray()
                    try:
                        while len(contents) < size:
                            requested = min(CHUNK_SIZE, size - len(contents))
                            chunk = afc_read(afc, handle, requested)
                            if not chunk or len(chunk) > requested:
                                raise UserError("The thumbnail was incomplete.")
                            contents.extend(chunk)
                    finally:
                        with contextlib.suppress(Exception):
                            afc.fclose(handle)
                    encoded = bytes(contents)
                    with Image.open(io.BytesIO(encoded)) as image:
                        if image.format != "JPEG" or image.width * image.height > 4_000_000:
                            continue
                        image.verify()
                    cache.mkdir(exist_ok=True)
                    with Image.open(io.BytesIO(encoded)) as image:
                        image = ImageOps.exif_transpose(image)
                        image.thumbnail((1000, 1000))
                        image.convert("RGB").save(target, "JPEG", quality=85)
                    return {"path": str(target), "kind": "photo", "thumbnail": True, "available": True}
        except Exception as error:
            LOG.debug("Optional iOS thumbnail unavailable: %s", type(error).__name__)
        if item["kind"] == "photo":
            return self._app_thumbnail(item)
        return unavailable

    def _app_thumbnail(self, item: dict) -> dict:
        unavailable = {"available": False}
        try:
            remote_path(item["path"], item["source"])
        except UserError:
            return unavailable
        if item["kind"] == "photo":
            if item["size"] > PREVIEW_LIMIT and not item.get("localPath"):
                return unavailable
            if not self._thumbnail_slots.acquire(blocking=False):
                return unavailable
            try:
                return {**self.preview(item), "available": True}
            except Exception:
                return unavailable
            finally:
                self._thumbnail_slots.release()
        import shutil
        import subprocess
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return unavailable
        identifier = hashlib.sha256(json.dumps([item["id"], item["modified"], item["size"]]).encode()).hexdigest()[:32]
        cache = self.cache_dir / item["deviceId"]
        target = cache / ("thumb-" + identifier + ".jpg")
        if target.is_file():
            return {"path": str(target), "kind": "photo", "thumbnail": True, "available": True}
        if not self._thumbnail_slots.acquire(blocking=False):
            return unavailable
        server = None
        stream = None
        temporary = None
        process = None
        process_session = None
        try:
            local = Path(item["localPath"]) if item.get("localPath") else None
            if local and not local.is_file():
                local = None
            if not local and not self._available(item["deviceId"]):
                return unavailable
            server = self._media_server()
            stream = server.register(item, local=local, budget=16 * 1024 * 1024, lifetime=10)
            cache.mkdir(exist_ok=True)
            temporary = cache / ("thumb-" + identifier + "." + uuid.uuid4().hex + ".tmp.jpg")
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "2", "-rw_timeout", "3000000",
                       "-probesize", "8000000", "-analyzeduration", "2000000", "-i", stream["url"], "-map", "0:v:0",
                       "-frames:v", "1", "-vf", "scale=640:640:force_original_aspect_ratio=decrease", "-threads", "2",
                       "-q:v", "3", "-y", str(temporary)]
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            process_session = server.attach_process(stream["url"], process)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                return unavailable
            if process.returncode or not temporary.is_file() or not 0 < temporary.stat().st_size <= THUMBNAIL_LIMIT:
                return unavailable
            from PIL import Image
            with Image.open(temporary) as image:
                if image.format != "JPEG" or image.width * image.height > 1_000_000:
                    return unavailable
                image.verify()
            os.replace(temporary, target)
            return {"path": str(target), "kind": "photo", "thumbnail": True, "available": True}
        except Exception as error:
            LOG.debug("Optional app thumbnail unavailable: %s", type(error).__name__)
            return unavailable
        finally:
            if process and process.poll() is None:
                with contextlib.suppress(Exception):
                    process.kill()
                    process.wait(timeout=3)
            if temporary:
                temporary.unlink(missing_ok=True)
            if stream and server:
                if process_session and process:
                    server.resource(process_session, process, False)
                server.revoke(stream["url"])
            self._thumbnail_slots.release()

    def _rename_device_under_mutation_lock(self, args: dict) -> dict:
        identifier = self._require_device(args.get("id"))
        name = str(args.get("name", "")).strip()[:100]
        if not name:
            raise UserError("Enter a name for this Device.")
        with self.lock:
            self._ensure_device_directories_locked()
            device = self._devices[identifier]
            before = self._location_preferences(device)
            old_history, old_cursor = copy.deepcopy(self._location_history), self._location_cursor
            old_folder = Path(device["defaultDownloadDir"])
            base = Path(device.get("defaultBaseDir") or old_folder.parent)
            new_folder = base / safe_filename(name)
            after = {**before, "alias": name, "defaultDownloadDir": str(new_folder), "defaultBaseDir": str(base),
                     "defaultPhoneNamed": True, "downloadSlug": new_folder.name,
                     "downloadDir": str(new_folder) if before["downloadDir"] == before["defaultDownloadDir"] else before["downloadDir"]}
            planned = {"id": uuid.uuid4().hex, "deviceId": identifier, "before": before, "after": after,
                       "label": "Rename Device and download folder", "createdAt": time.time(),
                       "renameFolder": str(old_folder) != str(new_folder)}
            device["alias"] = name
            try:
                self._rename_download_folder_locked(identifier, old_folder, new_folder, target_preferences=after,
                                                    journal_details={"mode": "new", "historyEntry": planned})
            except Exception:
                device.update(before)
                raise
            device.update(defaultDownloadDir=str(new_folder), defaultBaseDir=str(base), defaultPhoneNamed=True,
                          downloadSlug=new_folder.name)
            if before["downloadDir"] == before["defaultDownloadDir"]:
                device["downloadDir"] = str(new_folder)
            history = self._save_location_change_locked(identifier, before, "Rename Device and download folder",
                                                        entry_id=planned["id"], created_at=planned["createdAt"])
            if history:
                history["renameFolder"] = str(old_folder) != str(new_folder)
            self._reconcile_default_copies_locked()
            try:
                self._save_locked()
            except OSError:
                self._rename_download_folder_locked(identifier, new_folder, old_folder, target_preferences=before)
                device.update(before)
                self._location_history, self._location_cursor = old_history, old_cursor
                self._reconcile_default_copies_locked()
                with contextlib.suppress(OSError):
                    self.folder_rename_file.unlink(missing_ok=True)
                raise
        self.emit_state()
        return {"renamed": True, "changed": history is not None, "historyId": history.get("id") if history else None,
                "path": str(new_folder), "basePath": str(base)}

    def handle(self, command: str, args: dict | None = None, *, enqueue_generation: int | None = None) -> Any:
        args = args or {}
        if command == "state":
            return self.state()
        if command == "select_device":
            identifier = self._require_device(args.get("id"))
            with self.lock:
                self.selected_device = identifier
                self.current_scope = self.scope(identifier, "photos")
                self._save_locked()
            self._queried.pop(identifier, None)
            self.wake.set()
            self.emit_state()
            return self.state()
        if command == "rename_device":
            with self._destructive_lock:
                return self._rename_device_under_mutation_lock(args)
        if command == "scan":
            return self.request_scan(self._require_device(args.get("deviceId")), args.get("source", "photos"), args.get("appId"), args.get("path"))
        if command == "list_apps":
            return self.list_apps(self._require_device(args.get("deviceId")))
        if command == "files_home":
            return self.files_home(self._require_device(args.get("deviceId")), bool(args.get("refresh")))
        if command == "configure_files_home":
            return self.configure_files_home(self._require_device(args.get("deviceId")), args.get("appId"))
        if command == "ensure_files_home":
            return self.ensure_files_home(self._require_device(args.get("deviceId")))
        if command == "create_folder":
            return self.create_folder(self._require_device(args.get("deviceId")), args.get("appId"), args.get("path", "/Documents"), args.get("name"))
        if command == "preview_phone_folder_delete":
            return self.preview_phone_folder_delete(args.get("items", []))
        if command == "confirm_phone_folder_delete":
            with self._destructive_lock:
                return self.confirm_phone_folder_delete(args.get("token"))
        if command == "preview_phone_delete":
            return self.preview_phone_delete(args.get("items", []))
        if command == "confirm_phone_delete":
            with self._destructive_lock:
                return self.confirm_phone_delete(args.get("token"))
        if command == "preview_pc_cleanup":
            return self.preview_pc_cleanup(self._require_device(args.get("deviceId")))
        if command == "preview_pc_delete":
            return self.preview_pc_delete(self._require_device(args.get("deviceId")), args.get("items", []))
        if command == "confirm_pc_cleanup":
            with self._destructive_lock:
                return self.confirm_pc_cleanup(args.get("token"))
        if command == "confirm_pc_delete":
            with self._destructive_lock:
                return self.confirm_pc_delete(args.get("token"))
        if command == "get_pc_cleanup_history":
            return self._pc_history.state()
        if command in {"undo_pc_cleanup", "redo_pc_cleanup"}:
            with self._destructive_lock:
                return self.change_pc_cleanup_history(command == "undo_pc_cleanup", args.get("token"))
        if command == "get_cached_catalogue":
            identifier = self._require_device(args.get("deviceId"))
            source = args.get("source", "photos")
            app_id = args.get("appId")
            path = remote_path(args.get("path") or ("/DCIM" if source == "photos" else "/Documents"), source)
            scope = self.scope(identifier, source, app_id, path)
            with self.lock:
                self._ensure_device_directories_locked()
                self._reconcile_default_copies_locked()
                # This changes only the saved GUI scope. It never opens a phone
                # socket, so an offline library can be browsed immediately.
                if identifier == self.selected_device:
                    self.current_scope = scope
                    self._save_locked()
                items = copy.deepcopy(self._catalogues.get(scope, []))
            return {"deviceId": identifier, "source": source, "appId": app_id, "path": path, "items": items,
                    "needsLivePhotoScan": source == "photos" and self._needs_live_photo_scan(items)}
        if command == "download":
            return self.enqueue_download(self._require_device(args.get("deviceId")), args.get("items", []), args.get("destination"),
                                         sync=args.get("sync") if isinstance(args.get("sync"), bool) else None,
                                         enqueue_generation=enqueue_generation)
        if command == "queue_download":
            if not args.get("destination"):
                raise UserError("Choose a destination folder before queueing this transfer.")
            return self.enqueue_download(self._require_device(args.get("deviceId")), args.get("items", []), args.get("destination"),
                                         defer=True, enqueue_generation=enqueue_generation)
        if command == "resolve_transfer_items":
            return self.resolve_transfer_items(args.get("items", []), self._require_device(args["deviceId"]) if args.get("deviceId") else None)
        if command == "upload":
            return self.enqueue_upload(self._require_device(args.get("deviceId")), args.get("appId"), args.get("path", "/Documents"),
                                       args.get("localPaths", []), enqueue_generation=enqueue_generation)
        if command in {"cancel_job", "retry_job", "resume_job", "pause_job"}:
            return self.control_job(args.get("id"), command.split("_", 1)[0])
        if command == "start_queued_job":
            return self.start_queued_job(args.get("id"))
        if command == "clear_finished_jobs":
            return self.clear_finished_jobs()
        if command == "cancel_all_jobs":
            return self.cancel_all_jobs()
        if command == "clear_all_jobs":
            return self.clear_all_jobs()
        if command == "clear_cache":
            identifier = self._require_device(args.get("deviceId"))
            with self.lock:
                self._scan_generation += 1
                self._scan_request = None
                self.scanning = False
                self._catalogues = {key: items for key, items in self._catalogues.items() if json.loads(key)[0] != identifier}
                target = (self.cache_dir / identifier).resolve()
                if target.parent == self.cache_dir.resolve() and target.is_dir():
                    for image in target.glob("*.jpg"):
                        image.unlink(missing_ok=True)
                self._save_locked()
            self.emit_state()
            return {"cleared": True}
        if command in {"undo_download_location", "redo_download_location"}:
            with self._destructive_lock:
                return self._change_location_history(command == "undo_download_location", args.get("historyId"))
        if command == "set_default_download_dir":
            return self._set_global_download_root(args.get("path"))
        if command in {"set_download_dir", "reset_download_dir"}:
            with self.lock:
                self._ensure_device_directories_locked()
                identifier = self._destination_device_locked(args.get("deviceId"))
                if identifier is None:
                    raise UserError("Connect or select a Device before choosing its download folder.")
                default = self._devices[identifier]["defaultDownloadDir"]
            if command == "reset_download_dir":
                args = {**args, "path": default}
            if not args.get("path"):
                raise UserError("Choose a folder for downloads.")
            base_directory = Path(args.get("path", "")).expanduser().resolve()
            directory = (base_directory / self._phone_folder_name(self._devices[identifier])
                         if command == "set_default_download_dir" else base_directory)
            if command == "set_default_download_dir":
                with self.lock:
                    self._check_distinct_device_default_locked(identifier, directory)
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.is_dir():
                raise UserError("Choose a folder for downloads.")
            with self.lock:
                before = self._location_preferences(self._devices[identifier])
                old_history, old_cursor = copy.deepcopy(self._location_history), self._location_cursor
                if command == "set_default_download_dir":
                    self._check_distinct_device_default_locked(identifier, directory)
                    self._devices[identifier].update(defaultDownloadDir=str(directory), defaultDownloadKind="custom",
                                                     defaultBaseDir=str(base_directory), defaultPhoneNamed=True,
                                                     downloadSlug=directory.name)
                self._devices[identifier]["downloadDir"] = str(directory)
                history = self._save_location_change_locked(identifier, before,
                    "Change default download folder" if command == "set_default_download_dir" else "Change queue download folder")
                self._reconcile_default_copies_locked()
                self.download_dir = self._effective_download_dir_locked()
                try:
                    self._save_locked()
                except OSError:
                    self._devices[identifier].update(before)
                    self._location_history, self._location_cursor = old_history, old_cursor
                    self._reconcile_default_copies_locked()
                    self.download_dir = self._effective_download_dir_locked()
                    raise
            self.emit_state()
            return {"deviceId": identifier, "path": str(directory),
                    "defaultPath": self._devices[identifier]["defaultDownloadDir"], "permanent": command == "set_default_download_dir",
                    "basePath": self._devices[identifier].get("defaultBaseDir"),
                    "changed": history is not None, "historyId": history.get("id") if history else None}
        if command == "pair":
            identifier = self._require_device(args.get("deviceId"))
            with self._lockdown(identifier, autopair=True) as lockdown:
                if not lockdown.paired:
                    raise UserError("Accept Trust on the unlocked Device, then try again.")
            self._queried.pop(identifier, None)
            self.poll_devices()
            return {"paired": True}
        if command == "preview":
            return self.preview(args.get("item", {}), bool(args.get("compatible")), args.get("start", 0))
        if command == "video_metadata":
            return self.video_metadata(args.get("item", {}))
        if command in {"stream", "stream_file"}:
            return self.stream(args.get("item", {}), bool(args.get("compatible")), args.get("start", 0),
                               any_file=command == "stream_file", prefer_local=args.get("preferLocal", True) is not False)
        if command == "stop_stream":
            return self._media.revoke(args.get("url", "")) if self._media else {"stopped": False}
        if command == "thumbnail":
            return self.thumbnail(args.get("item", {}))
        if command == "diagnostics":
            return {"connection": copy.deepcopy(self.connection), "usb": self._usb_summary(),
                    "devices": self.state()["devices"], "backend": "pymobiledevice3 7.6.0 / Apple USB / AFC", "dataDir": str(self.data_dir)}
        raise UserError("That command is not supported.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--diagnostics-dir")
    parser.add_argument("--download-dir")
    args = parser.parse_args()
    # This default also bounds sockets created internally during discovery and
    # lockdown handshake, before we can reach the returned socket object.
    socket.setdefaulttimeout(12)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(name)s: %(message)s")
    logging.getLogger("pymobiledevice3").setLevel(logging.CRITICAL)
    output_lock = threading.Lock()

    def write(message: dict) -> None:
        with output_lock:
            sys.stdout.write(json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    engine = Engine(args.data_dir, emit=lambda event, data: write({"event": event, "data": data}),
                    diagnostics_dir=args.diagnostics_dir, download_dir=args.download_dir)
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="iphone-request")
    # A slow selection must not monopolize the command reader or the preview
    # pool. Bound pending setup requests as well as concurrent setup workers.
    queue_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="iphone-queue")
    queue_slots = threading.BoundedSemaphore(4)

    def respond(request: dict, enqueue_generation: int | None = None) -> None:
        identifier = request.get("id")
        try:
            result = engine.handle(request.get("command", ""), request.get("args"),
                                   enqueue_generation=enqueue_generation)
            write({"id": identifier, "result": result})
        except Exception as error:
            LOG.warning("Request failed: %s", type(error).__name__)
            write({"id": identifier, "error": public_error(error)})

    def prepare_queue_request(request: dict, generation: int) -> None:
        try:
            respond(request, generation)
        finally:
            queue_slots.release()

    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError()
                # Capture a trusted epoch at arrival, not when a queued worker
                # eventually starts. Renderer-supplied args cannot bypass Stop.
                if request.get("command") in {"download", "queue_download", "upload", "resolve_transfer_items"}:
                    generation = engine.queue_generation()
                    if queue_slots.acquire(blocking=False):
                        queue_pool.submit(prepare_queue_request, request, generation)
                    else:
                        write({"id": request.get("id"), "error": "Another selection is still being prepared. Wait for it to finish, or use Stop all."})
                elif request.get("command") in {"video_metadata", "set_default_download_dir", "undo_download_location", "redo_download_location", "rename_device", "pair", "list_apps", "preview", "thumbnail", "stream", "stream_file",
                                              "files_home", "ensure_files_home", "create_folder", "preview_phone_delete",
                                              "confirm_phone_delete", "preview_phone_folder_delete", "confirm_phone_folder_delete",
                                              "preview_pc_cleanup", "confirm_pc_cleanup", "preview_pc_delete", "confirm_pc_delete",
                                              "undo_pc_cleanup", "redo_pc_cleanup"}:
                    pool.submit(respond, request)
                else:
                    respond(request)
            except (ValueError, TypeError):
                write({"id": None, "error": "Send a JSON object containing id, command, and args."})
    finally:
        engine.close()
        queue_pool.shutdown(wait=False, cancel_futures=True)
        pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
