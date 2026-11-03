"""Protocol-independent transfer tests; never access a real iPhone."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from backend.engine import Engine, LEGACY_FOLDER_DENIED, SourceChangedError, UserError, afc_seek, atomic_json, commit_no_overwrite, device_key, remote_path, safe_filename, signature


class FakeAFC:
    def __init__(self):
        self.files = {}
        self.directories = {"/DCIM", "/DCIM/100APPLE", "/Documents"}
        self.links = set()
        self.handles = {}
        self.next_handle = 0
        self.modified = datetime(2026, 10, 9, tzinfo=timezone.utc)
        self.read_failures = 0
        self.disconnect_after_read = None
        self.after_read = None
        self.before_rename = None
        self.read_calls = []
        self.read_paths = []
        self.seek_calls = []
        self.write_paths = []
        self.rename_calls = []

    def add(self, path, contents):
        self.files[path] = bytearray(contents)

    def stat(self, path):
        if path in self.links:
            return {"st_ifmt": "S_IFLNK", "st_size": 0, "st_mtime": self.modified}
        if path in self.directories:
            return {"st_ifmt": "S_IFDIR", "st_size": 0, "st_mtime": self.modified}
        if path not in self.files:
            raise FileNotFoundError(path)
        return {"st_ifmt": "S_IFREG", "st_size": len(self.files[path]), "st_mtime": self.modified}

    def listdir(self, directory):
        children = []
        for path in list(self.files) + list(self.directories) + list(self.links):
            if Path(path).parent.as_posix() == directory and path != directory:
                children.append(Path(path).name)
        return [".", "..", *children]

    def exists(self, path):
        return path in self.files or path in self.directories or path in self.links

    def fopen(self, path, mode="r"):
        self.next_handle += 1
        handle = self.next_handle
        if mode == "w":
            self.files[path] = bytearray()
            self.write_paths.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        self.handles[handle] = [path, 0]
        return handle

    def fclose(self, handle):
        self.handles.pop(handle, None)

    def fseek(self, handle, offset):
        self.seek_calls.append(offset)
        self.handles[handle][1] = offset

    def fread(self, handle, count):
        if self.read_failures:
            self.read_failures -= 1
            raise ConnectionResetError("fake disconnect")
        path, offset = self.handles[handle]
        self.read_paths.append(path)
        self.read_calls.append((offset, count))
        result = bytes(self.files[path][offset:offset + count])
        self.handles[handle][1] += len(result)
        if self.after_read:
            self.after_read(path, offset, result)
        if self.disconnect_after_read is not None and len(self.read_calls) == self.disconnect_after_read:
            self.read_failures += 1
        return result

    def fwrite(self, handle, data):
        path, offset = self.handles[handle]
        end = offset + len(data)
        if end > len(self.files[path]):
            self.files[path].extend(b"\0" * (end - len(self.files[path])))
        self.files[path][offset:end] = data
        self.handles[handle][1] = end

    def rename(self, source, destination):
        if self.before_rename:
            self.before_rename(source, destination)
        # iPhone destination collisions must be detected before writing an
        # existing app document. The fake refuses any overwrite explicitly.
        if self.exists(destination):
            raise FileExistsError(destination)
        self.rename_calls.append((source, destination))
        self.files[destination] = self.files.pop(source)

    def rm_single(self, path):
        self.files.pop(path, None)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.afc = FakeAFC()
        self.events = []
        self.device_id = device_key("a-private-phone-serial")

        @contextlib.contextmanager
        def connector(device_id, source, app_id):
            self.assertEqual(device_id, self.device_id)
            yield self.afc

        self.engine = Engine(self.directory / "data", emit=lambda event, data: self.events.append((event, copy.deepcopy(data))), connector=connector, start=False)
        self.engine._devices[self.device_id] = {"id": self.device_id, "name": "Test iPhone", "alias": None, "online": True,
                                               "defaultDownloadDir": str(self.directory / "downloads"),
                                               "downloadDir": str(self.directory / "downloads"), "defaultDownloadKind": "custom"}
        self.engine.selected_device = self.device_id
        self.engine._apps[self.device_id] = [{"id": "test.documents", "name": "Documents", "bundleId": "test.documents"}]

    def tearDown(self):
        self.engine.close()
        self.temporary.cleanup()

    def download(self, content=b"abcdefghijk", name="clip.mov"):
        path = "/DCIM/100APPLE/" + name
        self.afc.add(path, content)
        item = self.engine._catalogue_item(self.device_id, "photos", None, path, self.afc.stat(path))
        scope = self.engine.scope(self.device_id, "photos")
        self.engine.current_scope = scope
        self.engine._catalogues[scope] = [item]
        self.engine.enqueue_download(self.device_id, [item], str(self.directory / "downloads"))
        return self.engine._jobs[-1]

    def upload(self, content=b"upload contents", name="clip.mov"):
        source = self.directory / name
        source.write_bytes(content)
        self.engine.enqueue_upload(self.device_id, "test.documents", "/Documents", [str(source)])
        return self.engine._jobs[-1]

    def test_safe_windows_filenames_and_remote_traversal(self):
        for unsafe in ("CON.mov", "LPT1.txt", "..", "../../clip:bad?.mov", "C:\\temp\\file.mov", "name. ", "\0bad.png"):
            name = safe_filename(unsafe)
            self.assertFalse(any(c in name for c in '/\\:<>"|?*\0'))
            self.assertFalse(name.endswith((".", " ")))
            self.assertLessEqual(len(name), 170)
        self.assertEqual(safe_filename("CON.mov"), "_CON.mov")
        for path in ("/Documents/../secret", "/DCIM/../Documents/file", "/private/foo", "/Documents\\foo", "/Documents/./foo"):
            with self.assertRaises(UserError):
                remote_path(path, "app")

    def test_full_download_preserves_contents_and_closes_handle(self):
        job = self.download()
        self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(Path(job["localPath"]).read_bytes(), b"abcdefghijk")
        self.assertFalse(Path(job["partPath"]).exists())
        self.assertFalse(self.afc.handles)
        self.assertEqual(self.engine.state()["catalogue"][0]["localPath"], job["localPath"])
        self.assertTrue(job["partPath"].endswith(f".eyecopy-{job['id']}.part"))

    def test_legacy_owned_local_partial_finishes_without_recopy_or_renaming_other_files(self):
        contents = b"synthetic legacy complete partial"
        job = self.download(contents)
        destination = Path(job["destination"])
        destination.mkdir()
        final = destination / job["name"]
        part = Path(str(final) + f".eyecopy-{job['id']}.part")
        part.write_bytes(contents)
        untouched = destination / "unrelated.part"
        untouched.write_bytes(b"untracked bytes kept")
        job.update(finalPath=str(final), partPath=str(part), sourceSignature=signature(self.afc.stat(job["item"]["path"])))
        self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(final.read_bytes(), contents)
        self.assertEqual(self.afc.read_calls, [])
        self.assertEqual(untouched.read_bytes(), b"untracked bytes kept")

    def test_resolve_prunes_deleted_local_copy_without_phone_read_or_drag_dom_event(self):
        job = self.download(b"deleted private fixture")
        self.engine.run_job(job)
        supplied = copy.deepcopy(job["item"])
        Path(job["localPath"]).unlink()
        self.engine._devices[self.device_id]["online"] = False
        self.events.clear()
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Cached resolution must not read phone")):
            result = self.engine.resolve_transfer_items([supplied], self.device_id)
        self.assertEqual(result["offlineDeviceIds"], [self.device_id])
        self.assertNotIn("localPath", result["items"][0])
        self.assertNotIn("localPath", self.engine._catalogues[self.engine.current_scope][0])
        self.assertFalse(self.events)
        restored = Engine(self.engine.data_dir, start=False)
        try:
            self.assertNotIn("localPath", restored.state()["catalogue"][0])
        finally:
            restored.close()

    def test_resolve_retains_valid_local_copy_even_when_phone_offline(self):
        job = self.download(b"usable offline original")
        self.engine.run_job(job)
        self.engine._devices[self.device_id]["online"] = False
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Local resolution must work offline")):
            result = self.engine.resolve_transfer_items([job["item"]])
        self.assertEqual(result["offlineDeviceIds"], [self.device_id])
        self.assertEqual(result["items"][0]["localPath"], job["localPath"])

    def test_shell_refresh_announces_only_closed_committed_completed_owned_output(self):
        job = self.download()
        def published(path):
            self.assertEqual(job["state"], "completed")
            self.assertEqual(path, job["localPath"])
            self.assertEqual(Path(path).read_bytes(), b"abcdefghijk")
            self.assertFalse(Path(job["partPath"]).exists())
            self.assertFalse(self.afc.handles)
            self.assertEqual(self.engine.state()["catalogue"][0]["localPath"], path)
            return True
        with mock.patch("backend.engine.notify_local_download", side_effect=published) as refresh:
            self.engine.run_job(job)
        refresh.assert_called_once_with(job["localPath"])
        failed = self.download(b"unfinished", "failed.mov")
        self.afc.read_failures = 1
        with mock.patch("backend.engine.notify_local_download") as refresh:
            self.engine.run_job(failed)
            refresh.assert_not_called()
        uploaded = self.upload()
        with mock.patch("backend.engine.notify_local_download") as refresh:
            self.engine.run_job(uploaded)
            refresh.assert_not_called()

    def test_shell_refresh_failure_cannot_fail_a_completed_download(self):
        job = self.download()
        with mock.patch("backend.engine.notify_local_download", side_effect=OSError("Shell unavailable")):
            self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(Path(job["localPath"]).read_bytes(), b"abcdefghijk")

    def test_destination_only_download_queue_never_opens_source_or_output_until_start(self):
        content = b"download after explicit start"
        path = "/DCIM/100APPLE/deferred.mov"
        self.afc.add(path, content)
        item = self.engine._catalogue_item(self.device_id, "photos", None, path, self.afc.stat(path))
        scope = self.engine.scope(self.device_id, "photos")
        self.engine.current_scope = scope
        self.engine._catalogues[scope] = [item]
        destination = self.directory / "selected destination"
        # The device can be connected, yet the transfer selector must skip the
        # queued destination. Even a direct run_job call must not read it.
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Deferred drop must not open AFC")), \
             mock.patch.object(self.engine, "_lockdown", side_effect=AssertionError("Deferred drop must not open a phone service")), \
             mock.patch("backend.engine.socket.socket", side_effect=AssertionError("Deferred drop must not open sockets")):
            result = self.engine.handle("queue_download", {
                "deviceId": self.device_id, "items": [{"id": item["id"], "name": "forged.mov", "path": "/DCIM/wrong.mov"}],
                "destination": str(destination),
            })
            job = self.engine._jobs[-1]
            self.assertEqual(result["jobIds"], [job["id"]])
            self.assertEqual(job["name"], "deferred.mov")
            self.assertEqual(job["item"]["path"], path)
            self.assertEqual(job["state"], "paused")
            self.assertTrue(job["awaitingStart"])
            self.assertEqual(job["bytes"], 0)
            self.assertEqual(job["destination"], str(destination.resolve()))
            self.assertIsNone(self.engine._next_work())
            self.engine.run_job(job)
            self.assertEqual(job["state"], "paused")
            self.assertFalse(destination.exists())
            self.assertFalse(self.afc.read_calls)
            self.assertFalse(self.afc.handles)
        self.engine.handle("resume_job", {"id": job["id"]})
        self.assertFalse(job["awaitingStart"])
        self.assertNotIn("message", job)
        self.assertEqual(self.engine._next_work(), ("transfer", job))
        self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(Path(job["localPath"]).read_bytes(), content)
        self.assertEqual(Path(job["localPath"]).parent, destination.resolve())

    def test_offline_destination_queue_persists_and_retry_explicitly_releases_it(self):
        existing = self.download(b"queue metadata", "offline.mov")
        item = existing["item"]
        self.engine._jobs.clear()
        self.engine._devices[self.device_id]["online"] = False
        destination = self.directory / "offline selected folder"
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Offline queue must use cache")):
            result = self.engine.handle("queue_download", {"deviceId": self.device_id, "items": [item], "destination": str(destination)})
        public = self.engine.public_jobs()[0]
        self.assertEqual(public["id"], result["jobIds"][0])
        self.assertTrue(public["awaitingStart"])
        self.assertEqual(public["destination"], str(destination.resolve()))
        self.assertNotIn("item", public)
        restored = Engine(self.engine.data_dir, connector=lambda *args: self.fail("Reload must not connect"), start=False)
        try:
            job = restored._jobs[0]
            self.assertEqual(job["state"], "paused")
            self.assertTrue(job["awaitingStart"])
            self.assertEqual(job["destination"], str(destination.resolve()))
            restored._devices[self.device_id]["online"] = True
            self.assertIsNone(restored._next_work())
            # The flag also prevents scheduling if a stale state is set queued.
            job["state"] = "queued"
            self.assertIsNone(restored._next_work())
            restored.save()
            reloaded = Engine(restored.data_dir, start=False)
            try:
                self.assertEqual(reloaded._jobs[0]["state"], "paused")
            finally:
                reloaded.close()
            restored.handle("retry_job", {"id": job["id"]})
            self.assertFalse(job["awaitingStart"])
            self.assertEqual(restored._next_work(), ("transfer", job))
        finally:
            restored.close()

    def test_cancel_destination_only_queue_is_immediate_without_source_io(self):
        existing = self.download()
        item = existing["item"]
        self.engine._jobs.clear()
        result = self.engine.handle("queue_download", {"deviceId": self.device_id, "items": [item], "destination": str(self.directory / "cancel folder")})
        job = self.engine._jobs[0]
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Cancel deferred queue must not read phone")):
            self.engine.handle("cancel_job", {"id": result["jobIds"][0]})
            self.assertEqual(job["state"], "cancelled")
            self.assertFalse(job["awaitingStart"])
            self.assertIsNone(self.engine._next_work())
        self.assertFalse(Path(job["destination"]).exists())
        with self.assertRaises(UserError):
            self.engine.handle("queue_download", {"deviceId": self.device_id, "items": [item]})

    def deferred_download(self):
        existing = self.download(b"guarded start original", "guarded.mov")
        item = existing["item"]
        self.engine._jobs.clear()
        self.engine.handle("queue_download", {"deviceId": self.device_id, "items": [item], "destination": str(self.directory / "guarded target")})
        return self.engine._jobs[0]

    def test_guarded_automatic_start_does_not_revive_cancelled_drop(self):
        job = self.deferred_download()
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Cancelled drop must not open phone")):
            self.engine.handle("cancel_job", {"id": job["id"]})
            self.assertEqual(self.engine.handle("start_queued_job", {"id": job["id"]}), {"changed": False})
            self.assertEqual(job["state"], "cancelled")
            self.assertIsNone(self.engine._next_work())
        self.assertFalse(Path(job["destination"]).exists())

    def test_guarded_automatic_start_releases_once_and_never_reactivates_started_job(self):
        job = self.deferred_download()
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Start command schedules; worker opens phone")):
            self.assertEqual(self.engine.handle("start_queued_job", {"id": job["id"]}), {"changed": True})
            self.assertEqual(job["state"], "queued")
            self.assertFalse(job["awaitingStart"])
            self.assertNotIn("message", job)
            self.assertEqual(self.engine.handle("start_queued_job", {"id": job["id"]}), {"changed": False})
            job["state"] = "transferring"
            self.assertEqual(self.engine.handle("start_queued_job", {"id": job["id"]}), {"changed": False})
            self.assertEqual(job["state"], "transferring")
        job["state"] = "queued"
        self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(Path(job["localPath"]).read_bytes(), b"guarded start original")
        reads = len(self.afc.read_calls)
        self.assertEqual(self.engine.handle("start_queued_job", {"id": job["id"]}), {"changed": False})
        self.assertEqual(len(self.afc.read_calls), reads)
        self.assertEqual(len(list(Path(job["destination"]).iterdir())), 1)

    def test_guarded_automatic_start_cannot_resume_ordinary_paused_job(self):
        job = self.download()
        self.engine.handle("pause_job", {"id": job["id"]})
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Ordinary paused job must stay paused")):
            self.assertEqual(self.engine.handle("start_queued_job", {"id": job["id"]}), {"changed": False})
            self.assertEqual(job["state"], "paused")
            self.assertIsNone(self.engine._next_work())
        self.assertFalse(Path(job["destination"]).exists())

    def test_clear_finished_history_preserves_files_cache_and_survives_reload(self):
        completed = self.download(b"keep editing original", "keep.mov")
        self.engine.run_job(completed)
        cancelled = self.download(b"cancelled", "cancelled.mov")
        self.engine.handle("cancel_job", {"id": cancelled["id"]})
        failed = self.download(b"failed", "failed.mov")
        failed["state"] = "failed"
        destination = Path(completed["destination"])
        unrelated = destination / "unrelated.eyecopy-keep.part"
        unrelated.write_bytes(b"keep unrelated partial")
        catalogue = copy.deepcopy(self.engine._catalogues)
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Clearing history must not open phone")), \
             mock.patch.object(self.engine, "_remove_local_part", side_effect=AssertionError("Clearing history must not delete files")):
            result = self.engine.handle("clear_finished_jobs", {})
        self.assertEqual(result, {"jobs": [], "cleared": 3})
        self.assertEqual(self.engine._catalogues, catalogue)
        self.assertEqual(Path(completed["localPath"]).read_bytes(), b"keep editing original")
        self.assertEqual(unrelated.read_bytes(), b"keep unrelated partial")
        self.assertNotIn(cancelled["id"], self.engine._requests)
        restored = Engine(self.engine.data_dir, start=False)
        try:
            self.assertEqual(restored.public_jobs(), [])
            self.assertEqual(restored._catalogues, catalogue)
            self.assertEqual(Path(completed["localPath"]).read_bytes(), b"keep editing original")
        finally:
            restored.close()
        self.assertEqual(self.events[-1], ("jobs", []))

    def test_clear_finished_preserves_work_active_terminal_and_cleanup_obligations(self):
        template = self.download()["item"]
        self.engine._jobs.clear()
        work = []
        for state in ("queued", "waiting", "transferring", "paused", "waiting_for_approval"):
            job = self.engine._new_job("keep.mov", "download", self.device_id, 1, item=template)
            job["state"] = state
            if state == "paused":
                job["awaitingStart"] = True
            work.append(job)
        active_terminal = self.engine._new_job("just finished.mov", "download", self.device_id, 1, item=template)
        active_terminal["state"] = "completed"
        self.engine._active_job = active_terminal["id"]
        cleanup = self.engine._new_job("cleanup.mov", "upload", self.device_id, 1)
        cleanup.update(state="cancelled", remoteCleanupPending=True, remoteTemp="/Documents/owned.part")
        removable = self.engine._new_job("finished.mov", "download", self.device_id, 1, item=template)
        removable["state"] = "completed"
        self.engine._jobs.extend([*work, active_terminal, cleanup, removable])
        before = copy.deepcopy(self.engine._jobs[:-1])
        try:
            with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Clear must not open phone")):
                result = self.engine.handle("clear_finished_jobs", {})
            self.assertEqual(result["cleared"], 1)
            self.assertEqual(self.engine._jobs, before)
            self.assertEqual([job["id"] for job in result["jobs"]], [job["id"] for job in before])
            self.assertFalse(any("item" in job for job in result["jobs"]))
            saved = json.loads(self.engine.state_file.read_text())
            self.assertEqual([job["id"] for job in saved["jobs"]], [job["id"] for job in before])
            self.engine._active_job = None
            second = self.engine.handle("clear_finished_jobs", {})
            self.assertEqual(second["cleared"], 1)
            self.assertIn(cleanup, self.engine._jobs)
            self.assertTrue(cleanup["remoteCleanupPending"])
        finally:
            self.engine._active_job = None

    def test_disconnect_preserves_partial_then_resumes_from_its_exact_size(self):
        job = self.download(b"abcdefghijkl")
        self.afc.disconnect_after_read = 1
        with mock.patch("backend.engine.CHUNK_SIZE", 4):
            self.engine.run_job(job)
            self.assertEqual(job["state"], "waiting")
            self.assertEqual(Path(job["partPath"]).read_bytes(), b"abcd")
            self.assertFalse(Path(job["finalPath"]).exists())
            self.afc.disconnect_after_read = None
            self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(self.afc.seek_calls, [4])
        self.assertEqual(Path(job["localPath"]).read_bytes(), b"abcdefghijkl")

    def test_download_does_not_overwrite_existing_local_file(self):
        job = self.download()
        directory = Path(job["destination"])
        directory.mkdir()
        existing = directory / "clip.mov"
        existing.write_bytes(b"keep this")
        self.engine.run_job(job)
        self.assertEqual(existing.read_bytes(), b"keep this")
        self.assertEqual(Path(job["localPath"]).name, "clip (2).mov")

    def test_collision_created_during_copy_is_preserved(self):
        job = self.download()
        def create_existing(path, offset, content):
            Path(job["finalPath"]).write_bytes(b"arrived while transferring")
        self.afc.after_read = create_existing
        self.engine.run_job(job)
        self.assertEqual((Path(job["destination"]) / "clip.mov").read_bytes(), b"arrived while transferring")
        self.assertEqual(Path(job["localPath"]).name, "clip (2).mov")

    def test_publication_retries_a_short_windows_lock_after_closing_phone_handle(self):
        job = self.download(b"fully copied original")
        failure = PermissionError(13, "file is in use")
        failure.winerror = 32
        attempts = []
        def publish(part, final):
            self.assertFalse(self.afc.handles)
            self.assertEqual(part.read_bytes(), b"fully copied original")
            attempts.append(final)
            if len(attempts) < 3:
                raise failure
            commit_no_overwrite(part, final)
        with mock.patch("backend.engine.commit_no_overwrite", side_effect=publish), \
             mock.patch("backend.engine.PUBLISH_RETRY_DELAYS", (0, 0)):
            self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(self.afc.read_calls), 1)
        self.assertNotIn("message", job)
        self.assertFalse(Path(job["partPath"]).exists())

    def test_atomic_json_retries_locked_replacement_and_keeps_unrelated_temporary(self):
        target = self.directory / "state.json"
        target.write_text('{"old":true}')
        unrelated = self.directory / "state.json.tmp"
        unrelated.write_bytes(b"not this writer's file")
        original = os.replace
        locked = PermissionError(13, "state reader denied rename")
        locked.winerror = 32
        attempts = []
        def replace(source, destination):
            attempts.append(Path(source))
            if len(attempts) == 1:
                raise locked
            original(source, destination)
        with mock.patch("backend.engine.os.replace", side_effect=replace), \
             mock.patch("backend.engine.STATE_RETRY_DELAYS", (0,)):
            atomic_json(target, {"new": True})
        self.assertEqual(json.loads(target.read_text()), {"new": True})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertNotEqual(attempts[0], unrelated)
        self.assertFalse(attempts[0].exists())
        self.assertEqual(unrelated.read_bytes(), b"not this writer's file")

    def test_progress_cache_lock_does_not_fail_valid_media_copy(self):
        job = self.download(b"copy despite progress-state lock")
        original = self.engine._save_locked
        calls = []
        error = PermissionError(13, "state reader lock")
        error.winerror = 32
        def save():
            calls.append(len(calls))
            if len(calls) == 3:
                raise error
            original()
        self.afc.after_read = lambda *args: setattr(self.engine, "_last_save", 0)
        with mock.patch.object(self.engine, "_save_locked", side_effect=save), \
             self.assertLogs("eyecopy", level="WARNING") as logs:
            self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertIsNone(job["error"])
        self.assertEqual(Path(job["localPath"]).read_bytes(), b"copy despite progress-state lock")
        self.assertIn("stage=state-save", " ".join(logs.output))
        self.assertEqual(json.loads(self.engine.state_file.read_text())["jobs"][0]["state"], "completed")

    def test_terminal_cache_lock_recovers_owned_publication_on_restart_without_duplicates_or_phone(self):
        job = self.download(b"already committed private original")
        original = self.engine._save_locked
        calls = []
        def save():
            calls.append(len(calls))
            if len(calls) >= 4:
                raise PermissionError(13, "terminal state is temporarily locked")
            original()
        with mock.patch.object(self.engine, "_save_locked", side_effect=save), \
             self.assertLogs("eyecopy", level="WARNING"):
            self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        persisted = json.loads(self.engine.state_file.read_text())["jobs"][0]
        self.assertEqual(persisted["state"], "transferring")
        self.assertTrue(persisted["publicationPending"])
        self.assertFalse(Path(job["partPath"]).exists())
        with mock.patch("backend.engine.Engine._afc", side_effect=AssertionError("Committed-file recovery must never read phone")):
            restored = Engine(self.engine.data_dir, start=False)
        try:
            recovered = restored._jobs[0]
            self.assertEqual(recovered["state"], "completed")
            self.assertEqual(recovered["localPath"], job["localPath"])
            self.assertEqual([p.name for p in Path(job["destination"]).iterdir()], ["clip.mov"])
            self.assertEqual(restored.state()["catalogue"][0]["localPath"], job["localPath"])
        finally:
            restored.close()

    def test_failed_legacy_full_copy_recovers_once_but_pauses_cancellation_and_changes_are_preserved(self):
        eligible = self.download(b"complete legacy file", "legacy.mov")
        with mock.patch("backend.engine.commit_no_overwrite", side_effect=PermissionError(13, "blocked")):
            self.engine.run_job(eligible)
        eligible.pop("publicationPending", None)
        eligible.pop("publicationIdentity", None)
        eligible["error"] = LEGACY_FOLDER_DENIED
        copies = []
        for state, name, message in (("paused", "paused.mov", LEGACY_FOLDER_DENIED),
                                     ("cancelled", "cancelled.mov", LEGACY_FOLDER_DENIED),
                                     ("failed", "changed.mov", "The source file changed.")):
            candidate = copy.deepcopy(eligible)
            candidate.update(id=__import__("uuid").uuid4().hex, state=state, name=name, error=message)
            candidate["finalPath"] = str(Path(eligible["destination"]) / name)
            candidate["partPath"] = candidate["finalPath"] + f".eyecopy-{candidate['id']}.part"
            Path(candidate["partPath"]).write_bytes(b"complete legacy file")
            copies.append(candidate)
        self.engine._jobs.extend(copies)
        self.engine.save()
        restored = Engine(self.engine.data_dir, connector=self.engine._connector, start=False)
        try:
            recovered = restored._jobs[0]
            self.assertEqual(recovered["state"], "waiting")
            self.assertTrue(recovered["publicationRecoveryAttempted"])
            self.assertEqual([j["state"] for j in restored._jobs[1:]], ["paused", "cancelled", "failed"])
            error = PermissionError(13, "still blocked")
            error.winerror = 32
            with mock.patch("backend.engine.commit_no_overwrite", side_effect=error), \
                 mock.patch("backend.engine.PUBLISH_RETRY_DELAYS", ()), \
                 mock.patch.object(self.afc, "fopen", side_effect=AssertionError("Recovery must use complete local copy")):
                restored.run_job(recovered)
            self.assertEqual(recovered["state"], "failed")
            again = Engine(self.engine.data_dir, start=False)
            try:
                self.assertEqual(again._jobs[0]["state"], "failed")
                self.assertTrue(again._jobs[0]["publicationRecoveryAttempted"])
                self.assertEqual(Path(recovered["partPath"]).read_bytes(), b"complete legacy file")
            finally:
                again.close()
        finally:
            restored.close()

    def test_zero_byte_original_download_is_published_without_invalid_phone_handle(self):
        job = self.download(b"", "empty.mov")
        with mock.patch.object(self.afc, "fopen", side_effect=AssertionError("Zero-byte download needs no file read")):
            self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(Path(job["localPath"]).read_bytes(), b"")

    @unittest.skipUnless(os.name == "nt", "native Windows sharing semantics")
    def test_actual_windows_read_handle_blocks_rename_and_retry_finishes_private_copy(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        job = self.download(b"private fixture bytes")
        held = []
        observed = []
        def open_reader(path, offset, content):
            handle = kernel.CreateFileW(job["partPath"], 0x80000000, 3, None, 3, 0x80, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            held.append(handle)
        def publish(part, final):
            try:
                commit_no_overwrite(part, final)
            except PermissionError as error:
                observed.append(error.winerror)
                self.assertTrue(kernel.CloseHandle(held.pop()))
                raise
        self.afc.after_read = open_reader
        try:
            with mock.patch("backend.engine.commit_no_overwrite", side_effect=publish), \
                 mock.patch("backend.engine.PUBLISH_RETRY_DELAYS", (0,)):
                self.engine.run_job(job)
        finally:
            for handle in held:
                kernel.CloseHandle(handle)
        self.assertEqual(observed, [32])
        self.assertEqual(job["state"], "completed")
        self.assertEqual(Path(job["localPath"]).read_bytes(), b"private fixture bytes")

    def test_failed_publication_preserves_complete_copy_and_retry_does_not_read_phone_bytes(self):
        job = self.download(b"complete preserved original")
        error = PermissionError(13, "rename blocked")
        error.winerror = 5
        with mock.patch("backend.engine.commit_no_overwrite", side_effect=error) as publish, \
             mock.patch("backend.engine.PUBLISH_RETRY_DELAYS", (0, 0)):
            self.engine.run_job(job)
        self.assertEqual(publish.call_count, 3)
        self.assertEqual(job["state"], "failed")
        self.assertIn("complete .part copy is preserved", job["error"])
        self.assertNotIn("writable destination", job["error"])
        self.assertEqual(job["failureDetails"], {"stage": "publish", "type": "PermissionError", "errno": 13, "winerror": 5})
        self.assertEqual(Path(job["partPath"]).read_bytes(), b"complete preserved original")
        self.assertTrue(self.engine.public_jobs()[0]["localPartRetained"])
        reads = len(self.afc.read_calls)
        self.engine.handle("retry_job", {"id": job["id"]})
        with mock.patch.object(self.afc, "fopen", side_effect=AssertionError("Complete retry must not open the phone file")):
            self.engine.run_job(job)
        self.assertEqual(len(self.afc.read_calls), reads)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(Path(job["localPath"]).read_bytes(), b"complete preserved original")

    def test_cancellation_during_publication_retry_is_responsive(self):
        job = self.download(b"cancel this private fixture")
        error = PermissionError(13, "rename blocked")
        error.winerror = 32
        def wait(delay):
            self.engine._requests[job["id"]] = "cancelled"
            return False
        with mock.patch("backend.engine.commit_no_overwrite", side_effect=error) as publish, \
             mock.patch.object(self.engine.stop, "wait", side_effect=wait):
            self.engine.run_job(job)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(job["state"], "cancelled")
        self.assertFalse(Path(job["partPath"]).exists())
        self.assertFalse(Path(job["finalPath"]).exists())

    def test_windows_directory_collision_uses_new_name_and_preserves_existing_target(self):
        job = self.download(b"download beside directory")
        def directory_collision(path, offset, content):
            Path(job["finalPath"]).mkdir(exist_ok=True)
        self.afc.after_read = directory_collision
        self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertTrue((Path(job["destination"]) / "clip.mov").is_dir())
        self.assertEqual(Path(job["localPath"]).name, "clip (2).mov")
        self.assertEqual(Path(job["localPath"]).read_bytes(), b"download beside directory")

    def test_clear_finished_preserves_partial_ownership_and_old_recovery_record_on_reload(self):
        job = self.download(b"recoverable original")
        with mock.patch("backend.engine.commit_no_overwrite", side_effect=PermissionError(13, "blocked")):
            self.engine.run_job(job)
        history = [self.engine._new_job("finished.mov", "download", self.device_id, 0, state="completed") for _ in range(501)]
        self.engine._jobs.extend(history)
        self.engine.save()
        saved = json.loads(self.engine.state_file.read_text())
        self.assertEqual(saved["jobs"][0]["id"], job["id"])
        restored = Engine(self.engine.data_dir, connector=self.engine._connector, start=False)
        try:
            result = restored.handle("clear_finished_jobs", {})
            self.assertEqual(result["cleared"], 500)
            self.assertEqual([j["id"] for j in result["jobs"]], [job["id"]])
            self.assertTrue(result["jobs"][0]["localPartRetained"])
            self.assertEqual(Path(job["partPath"]).read_bytes(), b"recoverable original")
            restored.handle("retry_job", {"id": job["id"]})
            recovered = restored._jobs[0]
            with mock.patch.object(self.afc, "fopen", side_effect=AssertionError("Recovery should not recopy source")):
                restored.run_job(recovered)
            self.assertEqual(recovered["state"], "completed")
        finally:
            restored.close()

    def test_changed_source_is_never_appended_to_an_old_partial(self):
        job = self.download(b"abcdefghijkl")
        self.afc.disconnect_after_read = 1
        with mock.patch("backend.engine.CHUNK_SIZE", 4):
            self.engine.run_job(job)
        self.afc.add(job["item"]["path"], b"a different longer video")
        self.engine.run_job(job)
        self.assertEqual(job["state"], "failed")
        self.assertIn("changed", job["error"])
        self.assertEqual(Path(job["partPath"]).read_bytes(), b"abcd")

    def test_truncated_source_never_becomes_a_completed_download(self):
        job = self.download(b"abcdefghijkl")
        def truncate(path, offset, content):
            if offset == 0:
                self.afc.files[path] = bytearray(b"abcd")
        self.afc.after_read = truncate
        with mock.patch("backend.engine.CHUNK_SIZE", 4):
            self.engine.run_job(job)
        self.assertEqual(job["state"], "failed")
        self.assertFalse(Path(job["finalPath"]).exists())

    def test_pause_then_resume_and_cancel_only_remove_owned_partial(self):
        job = self.download(b"abcdefghijkl")
        def pause(path, offset, content):
            if offset == 0:
                self.engine._requests[job["id"]] = "paused"
        self.afc.after_read = pause
        with mock.patch("backend.engine.CHUNK_SIZE", 4):
            self.engine.run_job(job)
        self.assertEqual(job["state"], "paused")
        self.assertEqual(Path(job["partPath"]).read_bytes(), b"abcd")
        self.afc.after_read = None
        self.engine.control_job(job["id"], "retry")
        self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        another = self.download(b"abcdefghijkl", "other.mov")
        self.afc.read_failures = 1
        self.engine.run_job(another)
        self.engine.control_job(another["id"], "cancel")
        self.assertEqual(another["state"], "cancelled")
        self.assertTrue(Path(job["localPath"]).is_file())

    def test_upload_stages_verifies_and_publishes_without_overwriting(self):
        self.afc.add("/Documents/clip.mov", b"original phone file")
        job = self.upload()
        self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(bytes(self.afc.files["/Documents/clip.mov"]), b"original phone file")
        self.assertEqual(bytes(self.afc.files["/Documents/clip (2).mov"]), b"upload contents")
        self.assertEqual(self.afc.write_paths, [f"/Documents/.eyecopy-{job['id']}.part"])
        self.assertEqual(self.afc.rename_calls[0][1], "/Documents/clip (2).mov")

    def test_upload_resume_only_writes_the_remaining_bytes(self):
        job = self.upload(b"abcdefghijkl")
        job["sourceSignature"] = {"size": 12, "mtimeNs": Path(job["sourcePath"]).stat().st_mtime_ns}
        job["remoteTemp"] = f"/Documents/.eyecopy-{job['id']}.part"
        self.afc.add(job["remoteTemp"], b"abcd")
        with mock.patch("backend.engine.CHUNK_SIZE", 4):
            self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(self.afc.seek_calls, [4])
        self.assertEqual(bytes(self.afc.files[job["remotePath"]]), b"abcdefghijkl")

    def test_upload_changed_local_source_leaves_no_published_file(self):
        job = self.upload(b"abcdefghijkl")
        job["sourceSignature"] = {"size": 10, "mtimeNs": 1}
        self.engine.run_job(job)
        self.assertEqual(job["state"], "failed")
        self.assertFalse(self.afc.rename_calls)

    def test_unshared_app_or_camera_upload_is_rejected(self):
        with self.assertRaises(UserError):
            self.engine.enqueue_upload(self.device_id, "com.apple.Photos", "/Documents", [str(self.directory / "clip.mov")])
        with self.assertRaises(UserError):
            self.engine.enqueue_upload(self.device_id, "test.documents", "/DCIM", [])

    def test_scan_skips_symlinks_and_retains_cache_after_disconnect(self):
        self.download()
        self.afc.links.add("/DCIM/link")
        self.engine._scan_generation = 1
        self.engine._scan(1, self.device_id, "photos", None, "/DCIM")
        catalogue = self.engine.state()["catalogue"]
        self.assertEqual(len(catalogue), 1)
        self.assertEqual(catalogue[0]["name"], "clip.mov")
        with mock.patch.object(self.afc, "listdir", side_effect=ConnectionResetError()):
            self.engine._scan(1, self.device_id, "photos", None, "/DCIM")
        self.assertEqual(self.engine.state()["catalogue"], catalogue)

    def test_symlink_ancestor_blocks_transfer(self):
        job = self.download()
        self.afc.links.add("/DCIM/100APPLE")
        self.engine.run_job(job)
        self.assertEqual(job["state"], "failed")
        self.assertFalse(self.afc.read_calls)

    def test_state_persists_alias_cache_jobs_without_phone_serial(self):
        job = self.download()
        self.engine.handle("rename_device", {"id": self.device_id, "name": "Editing camera"})
        self.engine._serials[self.device_id] = "a-private-phone-serial"
        self.engine.save()
        saved = self.engine.state_file.read_text()
        self.assertNotIn("a-private-phone-serial", saved)
        restored = Engine(self.engine.data_dir, start=False)
        self.assertEqual(restored._devices[self.device_id]["alias"], "Editing camera")
        self.assertFalse(restored._devices[self.device_id]["online"])
        self.assertEqual(restored._jobs[0]["state"], "waiting")
        self.assertNotIn("item", restored.public_jobs()[0])
        self.assertEqual(restored._apps[self.device_id][0]["id"], "test.documents")
        restored.close()

    def test_version_7_seek_adapter_builds_correct_protocol_payload(self):
        import struct
        from pymobiledevice3.services.afc import AfcOpcode
        class OldAFC:
            def _do_operation(self, opcode, payload):
                self.opcode, self.payload = opcode, payload
        service = OldAFC()
        afc_seek(service, 123, 9876543210)
        self.assertEqual(service.opcode, AfcOpcode.FILE_SEEK)
        self.assertEqual(struct.unpack("<QQq", service.payload), (123, 0, 9876543210))

    def test_completed_download_emits_updated_catalogue_for_native_drag(self):
        job = self.download()
        self.engine.run_job(job)
        states = [data for event, data in self.events if event == "state"]
        self.assertTrue(states[-1]["catalogue"][0]["localPath"])

    def test_clear_cache_keeps_alias_downloads_and_pairing_material(self):
        job = self.download()
        self.engine.run_job(job)
        self.engine._devices[self.device_id]["alias"] = "Remember me"
        cached = self.engine.cache_dir / self.device_id
        cached.mkdir()
        (cached / "thumbnail.jpg").write_bytes(b"thumbnail")
        self.engine.handle("clear_cache", {"deviceId": self.device_id})
        self.assertEqual(self.engine.state()["catalogue"], [])
        self.assertTrue(Path(job["localPath"]).is_file())
        self.assertEqual(self.engine._devices[self.device_id]["alias"], "Remember me")

    def test_preview_remote_limit_blocks_large_automatic_copy(self):
        job = self.download(b"small", "large.heic")
        self.engine._catalogues[self.engine.current_scope][0]["size"] = 26 * 1024 * 1024
        with self.assertRaises(UserError):
            self.engine.preview(job["item"])
        self.assertFalse(self.afc.read_calls)

    def test_small_photo_preview_creates_thumbnail_and_reuses_cache(self):
        from PIL import Image
        encoded = io.BytesIO()
        Image.new("RGB", (1200, 600), "blue").save(encoded, "JPEG")
        job = self.download(encoded.getvalue(), "photo.jpg")
        preview = self.engine.preview(job["item"])
        self.assertTrue(preview["thumbnail"])
        with Image.open(preview["path"]) as thumbnail:
            self.assertLessEqual(max(thumbnail.size), 1000)
        self.afc.read_calls.clear()
        self.assertEqual(self.engine.preview(job["item"]), preview)
        self.assertFalse(self.afc.read_calls)

    def test_diagnostics_prefers_fresh_appdata_report(self):
        report = {"CapturedAtUtc": "today", "Phones": [{"DeviceId": self.device_id, "UsbConnection": [{"ConnectionStatus": 1, "V2QuerySucceeded": True, "NegotiatedSpeed": "SuperSpeedPlus", "SignalingRate": "10 Gbit/s"}]}]}
        (self.engine.data_dir / "usb-diagnostics.json").write_text(json.dumps(report))
        self.assertEqual(self.engine.state()["usb"]["signalingRate"], "10 Gbit/s")
        self.assertEqual(self.engine.state()["usb"]["capturedAt"], "today")

    def test_cached_folder_scope_is_available_offline_without_opening_usb(self):
        self.engine._devices[self.device_id]["online"] = False
        item = self.engine._catalogue_item(self.device_id, "app", "test.documents", "/Documents/nested/clip.mov",
                                           {"st_ifmt": "S_IFREG", "st_size": 123, "st_mtime": self.afc.modified})
        self.assertEqual(item["parentPath"], "/Documents/nested")
        scope = self.engine.scope(self.device_id, "app", "test.documents", "/Documents/nested")
        self.engine._catalogues[scope] = [item]
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("USB must not open")):
            result = self.engine.handle("get_cached_catalogue", {"deviceId": self.device_id, "source": "app", "appId": "test.documents", "path": "/Documents/nested"})
        self.assertEqual(result["items"], [item])
        self.assertEqual(self.engine.state()["currentPath"], "/Documents/nested")
        result["items"][0]["name"] = "renderer copy"
        self.assertEqual(self.engine._catalogues[scope][0]["name"], "clip.mov")

    def test_usb_diagnostics_matches_selected_phone_and_marks_offline_history(self):
        other = device_key("another phone")
        report = {"CapturedAtUtc": "2026-10-09T10:00:00Z", "Phones": [
            {"DeviceId": other, "UsbConnection": [{"ConnectionStatus": 1, "V2QuerySucceeded": True, "NegotiatedSpeed": "HighSpeed", "SignalingRate": "480 Mbit/s"}]},
            {"DeviceId": self.device_id, "UsbConnection": [{"ConnectionStatus": 1, "V2QuerySucceeded": True, "NegotiatedSpeed": "SuperSpeedPlus", "SignalingRate": "10 Gbit/s"}]}
        ]}
        (self.engine.data_dir / "usb-diagnostics.json").write_text(json.dumps(report))
        self.assertEqual(self.engine.state()["usb"]["signalingRate"], "10 Gbit/s")
        self.engine._devices[self.device_id]["online"] = False
        self.assertTrue(self.engine.state()["usb"]["historical"])
        self.assertIn("offline", self.engine.state()["usb"]["message"])
        self.engine.selected_device = device_key("not present in report")
        self.assertFalse(self.engine.state()["usb"]["verified"])
        self.engine.selected_device = None
        self.assertFalse(self.engine.state()["usb"]["verified"])

    def test_waiting_job_resumes_for_its_phone_while_another_phone_is_selected(self):
        job = self.download()
        job["state"] = "waiting"
        self.engine.selected_device = device_key("another phone being browsed")
        work = self.engine._next_work()
        self.assertEqual(work[0], "transfer")
        self.assertIs(work[1], job)

    def test_cancelled_inactive_upload_cleans_only_its_owned_phone_partial(self):
        job = self.upload()
        job.update(state="paused", remoteTemp=f"/Documents/.eyecopy-{job['id']}.part")
        self.afc.add(job["remoteTemp"], b"partial upload")
        self.afc.add("/Documents/existing-document.txt", b"preserve me")
        self.engine.control_job(job["id"], "cancel")
        self.assertTrue(job["remoteCleanupPending"])
        self.assertEqual(self.engine._next_work()[0], "cleanup")
        self.engine._cleanup_upload(job)
        self.assertFalse(job.get("remoteCleanupPending"))
        self.assertFalse(self.afc.exists(job["remoteTemp"]))
        self.assertEqual(bytes(self.afc.files["/Documents/existing-document.txt"]), b"preserve me")
        self.assertEqual(job["state"], "cancelled")

    def test_cleanup_failure_stays_pending_and_retry_skips_cleanup(self):
        job = self.upload()
        job.update(state="waiting", remoteTemp=f"/Documents/.eyecopy-{job['id']}.part")
        self.afc.add(job["remoteTemp"], b"partial")
        self.engine.control_job(job["id"], "cancel")
        with mock.patch.object(self.afc, "rm_single", side_effect=ConnectionResetError()):
            self.engine._cleanup_upload(job)
        self.assertTrue(job["remoteCleanupPending"])
        self.assertGreater(job["cleanupRetryAfter"], 0)
        self.assertIsNone(self.engine._next_work())
        self.engine.control_job(job["id"], "retry")
        self.assertFalse(job.get("remoteCleanupPending"))
        self.assertEqual(self.engine._next_work()[0], "transfer")

    def test_cleanup_refuses_paths_or_symlinks_not_owned_by_the_job(self):
        job = self.upload()
        job.update(state="cancelled", remoteCleanupPending=True, remoteTemp="/Documents/important.mov")
        self.afc.add(job["remoteTemp"], b"do not remove")
        self.engine._cleanup_upload(job)
        self.assertTrue(self.afc.exists("/Documents/important.mov"))
        self.assertTrue(job["remoteCleanupPending"])
        job["remoteTemp"] = f"/Documents/.eyecopy-{job['id']}.part"
        self.afc.links.add(job["remoteTemp"])
        self.engine._cleanup_upload(job)
        self.assertIn(job["remoteTemp"], self.afc.links)

    def test_reload_clears_monotonic_retry_deadlines(self):
        job = self.upload()
        job.update(state="cancelled", remoteCleanupPending=True, retryAfter=987654321, cleanupRetryAfter=987654321)
        self.engine.save()
        restored = Engine(self.engine.data_dir, start=False)
        self.assertNotIn("retryAfter", restored._jobs[0])
        self.assertNotIn("cleanupRetryAfter", restored._jobs[0])
        self.assertTrue(restored._jobs[0]["remoteCleanupPending"])
        restored.close()

    def jpeg_thumbnail(self):
        from PIL import Image
        encoded = io.BytesIO()
        Image.new("RGB", (576, 324), "orange").save(encoded, "JPEG")
        return encoded.getvalue()

    def test_video_thumbnail_reads_only_small_jpeg_and_reuses_cache_offline(self):
        job = self.download(b"do not download this video for a thumbnail")
        thumbnail_path = "/PhotoData/Thumbnails/V2" + job["item"]["path"] + "/5005.JPG"
        self.afc.add(thumbnail_path, self.jpeg_thumbnail())
        result = self.engine.handle("thumbnail", {"item": job["item"]})
        self.assertTrue(result["available"])
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(self.afc.read_paths, [thumbnail_path])
        self.engine._devices[self.device_id]["online"] = False
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Offline thumbnails must use cache")):
            self.assertEqual(self.engine.thumbnail(job["item"]), result)

    def test_missing_and_oversize_thumbnail_are_optional_and_fallback_is_supported(self):
        job = self.download()
        prefix = "/PhotoData/Thumbnails/V2" + job["item"]["path"]
        self.assertFalse(self.engine.thumbnail(job["item"])["available"])
        self.afc.add(prefix + "/5005.JPG", b"x" * (2 * 1024 * 1024 + 1))
        self.assertFalse(self.engine.thumbnail(job["item"])["available"])
        self.assertFalse(self.afc.read_paths)
        self.afc.add(prefix + "/5003.JPG", self.jpeg_thumbnail())
        self.assertTrue(self.engine.thumbnail(job["item"])["available"])
        self.assertEqual(self.afc.read_paths, [prefix + "/5003.JPG"])

    def test_app_thumbnail_and_outside_dcim_path_never_open_a_phone_socket(self):
        metadata = {"st_ifmt": "S_IFREG", "st_size": 1, "st_mtime": self.afc.modified}
        app = self.engine._catalogue_item(self.device_id, "app", "test.documents", "/Documents/clip.mp4", metadata)
        outside = self.engine._catalogue_item(self.device_id, "photos", None, "/Documents/private.jpg", metadata)
        self.engine._catalogues["test-items"] = [app, outside]
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Only safe DCIM thumbnails are allowed")):
            self.assertFalse(self.engine.thumbnail(app)["available"])
            self.assertFalse(self.engine.thumbnail(outside)["available"])


if __name__ == "__main__":
    unittest.main()
