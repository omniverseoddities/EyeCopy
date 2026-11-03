"""Default-library consistency checks using temporary media, never a real phone."""
import contextlib
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.engine import Engine, UserError, device_key
from backend.test_backend import FakeAFC


class DownloadLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.afc = FakeAFC()
        self.phone = device_key("synthetic-download-library-phone")

        @contextlib.contextmanager
        def connector(*args):
            yield self.afc

        self.engine = Engine(self.root / "data", start=False, connector=connector,
                             download_dir=self.root / "Videos" / "Previous Documents")
        self.engine._devices[self.phone] = {"id": self.phone, "name": "Sample Device", "online": True}
        self.engine.selected_device = self.phone
        self.engine.current_scope = self.engine.scope(self.phone, "photos")

    def tearDown(self):
        self.engine.close()
        self.temporary.cleanup()

    def photo(self, name="photo.JPG", contents=b"synthetic original"):
        path = "/DCIM/100APPLE/" + name
        self.afc.add(path, contents)
        item = self.engine._catalogue_item(self.phone, "photos", None, path, self.afc.stat(path))
        self.engine._catalogues.setdefault(self.engine.current_scope, []).append(item)
        return item

    def copy(self, item, destination=None, sync=None):
        result = self.engine.enqueue_download(self.phone, [item], destination, sync=sync)
        jobs = [job for job in self.engine._jobs if job["id"] in result["jobIds"]]
        for job in jobs:
            self.engine.run_job(job)
            self.assertEqual(job["state"], "completed")
        return result, jobs

    def restore(self):
        self.engine.save()
        return Engine(self.engine.data_dir, start=False, download_dir=self.engine.download_root)

    def test_phone_name_default_preserves_case_spaces_and_is_collision_safe(self):
        first = self.engine.state()["devices"][0]
        self.assertEqual(Path(first["defaultDownloadDir"]), self.engine.download_root / "Sample Device")
        other = device_key("synthetic-second-phone")
        self.engine._devices[other] = {"id": other, "name": "Sample Device", "online": False}
        self.engine.state()
        self.assertEqual(Path(self.engine._devices[other]["defaultDownloadDir"]).name, "Sample Device (2)")
        self.engine._devices[self.phone].update(name="Changed phone name", alias="Changed alias")
        self.assertEqual(Path(self.engine.state()["devices"][0]["defaultDownloadDir"]).name, "Changed alias")

    def test_numbered_default_migrates_pointer_without_moving_old_files(self):
        old = self.engine.download_root / "iphone-1"
        old.mkdir(parents=True)
        original = old / "existing-original.mov"
        original.write_bytes(b"preserved existing download")
        self.engine._devices[self.phone].update(downloadNumber=1, defaultDownloadDir=str(old), downloadDir=str(old))
        self.engine.state()
        device = self.engine._devices[self.phone]
        self.assertEqual(Path(device["defaultDownloadDir"]).name, "Sample Device")
        self.assertEqual(device["downloadDir"], device["defaultDownloadDir"])
        self.assertEqual(device["previousDownloadDir"], str(old))
        self.assertEqual(original.read_bytes(), b"preserved existing download")

    def test_named_default_migration_keeps_temporary_queue_override(self):
        old = self.engine.download_root / "iphone-1"
        override = str(self.root / "temporary-target")
        self.engine._devices[self.phone].update(defaultDownloadDir=str(old), downloadDir=override)
        self.engine.state()
        self.assertEqual(Path(self.engine._devices[self.phone]["defaultDownloadDir"]).name, "Sample Device")
        self.assertEqual(self.engine._devices[self.phone]["downloadDir"], override)

    def test_permanent_custom_default_survives_reset_reconnect_and_restart(self):
        custom = self.root / "permanent-originals"
        self.engine.handle("set_default_download_dir", {"deviceId": self.phone, "path": str(custom)})
        self.engine.handle("set_download_dir", {"deviceId": self.phone, "path": str(self.root / "one-off")})
        result = self.engine.handle("reset_download_dir", {"deviceId": self.phone})
        self.assertEqual(result["path"], str(custom / "EyeCopy" / "Sample Device"))
        self.engine._devices[self.phone].update(name="New phone label", online=False)
        restored = self.restore()
        try:
            self.assertEqual(restored._devices[self.phone]["defaultDownloadDir"], str(custom / "EyeCopy" / "New phone label"))
            self.assertEqual(restored.state()["downloadDir"], str(custom / "EyeCopy" / "New phone label"))
        finally:
            restored.close()

    def test_user_chosen_numbered_folder_is_not_migrated(self):
        custom = self.root / "iphone-42"
        self.engine.handle("set_default_download_dir", {"path": str(custom)})
        restored = self.restore()
        try:
            self.assertEqual(restored.state()["downloadDir"], str(custom / "EyeCopy" / "Sample Device"))
        finally:
            restored.close()

    def test_photos_default_download_marks_on_pc_survives_scan_and_clear_queue(self):
        item = self.photo()
        _, jobs = self.copy(item)
        local = jobs[0]["localPath"]
        self.assertEqual(self.engine.state()["catalogue"][0]["localPath"], local)
        self.engine.clear_finished_jobs()
        self.engine._scan_generation = 1
        self.engine._scan(1, self.phone, "photos", None, "/DCIM")
        self.assertEqual(self.engine.state()["catalogue"][0]["localPath"], local)
        restored = self.restore()
        try:
            self.assertEqual(restored.state()["catalogue"][0]["localPath"], local)
        finally:
            restored.close()

    def test_external_download_has_queue_result_but_no_pc_tag_or_library_record(self):
        item = self.photo()
        external = self.root / "editing-target"
        _, jobs = self.copy(item, str(external))
        self.assertTrue(Path(jobs[0]["localPath"]).is_file())
        self.assertNotIn("localPath", self.engine.state()["catalogue"][0])
        self.assertEqual(self.engine._download_records, {})
        self.engine.clear_finished_jobs()
        restored = self.restore()
        try:
            self.assertNotIn("localPath", restored.state()["catalogue"][0])
            self.assertEqual(restored._download_records, {})
        finally:
            restored.close()

    def test_refresh_does_not_stat_external_or_former_default_paths(self):
        item = self.photo()
        _, jobs = self.copy(item)
        old = Path(jobs[0]["localPath"])
        self.engine.handle("set_default_download_dir", {"path": str(self.root / "new-default")})
        self.engine._catalogues[self.engine.current_scope][0]["localPath"] = str(self.root / "external" / "photo.JPG")
        real_lstat = Path.lstat
        def guard(path, *args, **kwargs):
            self.assertNotEqual(path, old)
            self.assertNotEqual(path, self.root / "external" / "photo.JPG")
            return real_lstat(path, *args, **kwargs)
        with mock.patch.object(Path, "lstat", autospec=True, side_effect=guard):
            self.assertNotIn("localPath", self.engine.state()["catalogue"][0])
        self.assertTrue(old.is_file())

    def test_deleted_default_copy_loses_tag_on_offline_refresh_and_redownloads(self):
        item = self.photo()
        _, jobs = self.copy(item)
        original = Path(jobs[0]["localPath"])
        original.unlink()
        self.engine._devices[self.phone]["online"] = False
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("Refresh must be local only")):
            self.assertNotIn("localPath", self.engine.state()["catalogue"][0])
            self.assertNotIn("localPath", self.engine.resolve_transfer_items([item])["items"][0])
        self.engine._devices[self.phone]["online"] = True
        result, jobs = self.copy(item)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(Path(jobs[0]["localPath"]), original)

    def test_sync_skips_existing_default_original_without_new_phone_reads(self):
        item = self.photo()
        _, jobs = self.copy(item)
        before = len(self.afc.read_calls)
        result, second = self.copy(item)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(second, [])
        self.assertEqual(len(self.afc.read_calls), before)
        self.assertEqual(list(Path(jobs[0]["destination"]).glob("*.JPG")), [Path(jobs[0]["localPath"])])

    def test_sync_skips_duplicate_queued_selection_and_explicit_drag_allows_duplicate(self):
        item = self.photo()
        first = self.engine.enqueue_download(self.phone, [item])
        second = self.engine.enqueue_download(self.phone, [item])
        self.assertEqual(second["jobIds"], [])
        self.assertEqual(second["inProgressJobIds"], first["jobIds"])
        self.engine.run_job(self.engine._jobs[0])
        _, jobs = self.copy(item, self.engine._devices[self.phone]["defaultDownloadDir"], sync=False)
        self.assertEqual(Path(jobs[0]["localPath"]).name, "photo (2).JPG")

    def test_external_duplicates_are_allowed_and_do_not_change_default_tag(self):
        item = self.photo()
        _, original = self.copy(item)
        target = str(self.root / "explicit-drop")
        self.copy(item, target, sync=False)
        _, duplicate = self.copy(item, target, sync=False)
        self.assertEqual(Path(duplicate[0]["localPath"]).name, "photo (2).JPG")
        self.assertEqual(self.engine.state()["catalogue"][0]["localPath"], original[0]["localPath"])

    def test_changed_source_metadata_and_replaced_local_copy_do_not_match(self):
        item = self.photo()
        _, jobs = self.copy(item)
        Path(jobs[0]["localPath"]).write_bytes(b"replacement")
        self.assertNotIn("localPath", self.engine.state()["catalogue"][0])
        # An existing name collision is protected; syncing gets a new original.
        result, newer = self.copy(item)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(Path(newer[0]["localPath"]).name, "photo (2).JPG")
        item["modified"] = "changed-source-metadata"
        self.assertNotIn("localPath", self.engine.state()["catalogue"][0])

    def test_live_photo_sync_downloads_missing_component_only(self):
        photo = self.photo("LIVE.JPG", b"live-photo-still")
        video = self.photo("LIVE.MOV", b"live-photo-motion")
        self.engine._catalogues[self.engine.current_scope].remove(video)
        photo["livePhoto"] = {"verified": True, "video": video}
        first, jobs = self.copy(photo)
        self.assertEqual(first["queued"], 2)
        self.assertTrue(photo["localPath"])
        self.assertTrue(photo["livePhoto"]["video"]["localPath"])
        motion = Path(jobs[1]["localPath"])
        motion.unlink()
        self.engine.state()
        second, retry = self.copy(photo)
        self.assertEqual(second["queued"], 1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(Path(retry[0]["localPath"]), motion)

    def test_location_undo_redo_changes_global_preferences_only_and_is_persistently_guarded(self):
        item = self.photo()
        _, jobs = self.copy(item)
        old = self.engine._devices[self.phone]["defaultDownloadDir"]
        original = Path(jobs[0]["localPath"])
        custom = str(self.root / "chosen-default")
        result = self.engine.handle("set_default_download_dir", {"path": custom})
        self.assertTrue(result["changed"])
        current = Path(custom) / "EyeCopy" / "Sample Device" / original.name
        self.assertNotIn("localPath", self.engine.state()["catalogue"][0])
        self.assertTrue(original.is_file())
        self.assertFalse(current.exists())
        with self.assertRaises(UserError):
            self.engine.handle("undo_download_location", {"historyId": "wrong-action"})
        self.engine.handle("undo_download_location", {"historyId": result["historyId"]})
        self.assertEqual(self.engine._devices[self.phone]["defaultDownloadDir"], old)
        self.assertEqual(self.engine.state()["catalogue"][0]["localPath"], str(original))
        restored = self.restore()
        try:
            self.assertTrue(restored.state()["locationHistory"]["canRedo"])
            restored.handle("redo_download_location", {"historyId": result["historyId"]})
            self.assertEqual(restored._devices[self.phone]["defaultDownloadDir"], str(Path(custom) / "EyeCopy" / "Sample Device"))
            self.assertTrue(original.is_file())
        finally:
            restored.close()

    def test_noop_reset_adds_no_history_and_new_action_invalidates_redo(self):
        first = self.engine.handle("set_default_download_dir", {"path": str(self.root / "first")})
        count = len(self.engine._location_history)
        result = self.engine.handle("reset_download_dir", {})
        self.assertFalse(result["changed"])
        self.assertEqual(len(self.engine._location_history), count)
        self.engine.handle("undo_download_location", {"historyId": first["historyId"]})
        self.engine.handle("set_default_download_dir", {"path": str(self.root / "second")})
        self.assertFalse(self.engine.state()["locationHistory"]["canRedo"])

    def test_blocked_settings_save_rolls_back_location_and_history(self):
        baseline = self.engine.state()["downloadDir"]
        with mock.patch.object(self.engine, "_save_locked", side_effect=PermissionError("synthetic cache lock")):
            with self.assertRaises(PermissionError):
                self.engine.handle("set_default_download_dir", {"path": str(self.root / "unsaved")})
        self.assertEqual(self.engine.state()["downloadDir"], baseline)
        self.assertEqual(self.engine._location_history, [])
        result = self.engine.handle("set_default_download_dir", {"path": str(self.root / "saved")})
        cursor = self.engine._location_cursor
        with mock.patch.object(self.engine, "_save_locked", side_effect=PermissionError("synthetic cache lock")):
            with self.assertRaises(PermissionError):
                self.engine.handle("undo_download_location", {"historyId": result["historyId"]})
        self.assertEqual(self.engine._location_cursor, cursor)
        self.assertEqual(self.engine.state()["downloadDir"], str(self.root / "saved" / "EyeCopy" / "Sample Device"))

    def test_phone_rename_moves_current_folder_and_records_and_supports_undo_redo(self):
        item = self.photo()
        _, jobs = self.copy(item)
        original = Path(jobs[0]["localPath"])
        default_base = self.engine.download_root
        result = self.engine.handle("rename_device", {"name": "My iPhone 17 Pro Max"})
        current = default_base / "My iPhone 17 Pro Max" / original.name
        self.assertFalse(original.exists())
        self.assertEqual(current.read_bytes(), b"synthetic original")
        self.assertEqual(self.engine.state()["catalogue"][0]["localPath"], str(current))
        self.assertIn(str(current), self.engine._download_records)
        self.assertEqual(jobs[0]["localPath"], str(current))
        self.engine.handle("undo_download_location", {"historyId": result["historyId"]})
        self.assertIsNone(self.engine._devices[self.phone].get("alias"))
        self.assertTrue(original.is_file())
        self.assertEqual(self.engine.state()["catalogue"][0]["localPath"], str(original))
        self.engine.handle("redo_download_location", {"historyId": result["historyId"]})
        self.assertEqual(self.engine._devices[self.phone]["alias"], "My iPhone 17 Pro Max")
        self.assertTrue(current.is_file())

    def test_global_parent_stays_fixed_while_current_phone_folder_renames_only(self):
        item = self.photo()
        _, jobs = self.copy(item)
        old = Path(jobs[0]["localPath"])
        parent = self.root / "New persistent parent"
        chosen = self.engine.handle("set_default_download_dir", {"path": str(parent)})
        self.assertEqual(chosen["basePath"], str(parent))
        self.assertEqual(chosen["rootPath"], str(parent / "EyeCopy"))
        self.assertTrue(old.exists())
        _, current_jobs = self.copy(item)
        current = Path(current_jobs[0]["localPath"])
        self.assertEqual(current.parent, parent / "EyeCopy" / "Sample Device")
        self.engine.handle("rename_device", {"name": "Video Phone"})
        self.assertEqual(self.engine._devices[self.phone]["defaultBaseDir"], str(parent / "EyeCopy"))
        self.assertTrue((parent / "EyeCopy" / "Video Phone" / old.name).is_file())
        self.assertTrue(old.is_file())
        self.engine.handle("reset_download_dir", {})
        self.assertEqual(self.engine.state()["downloadDir"], str(parent / "EyeCopy" / "Video Phone"))

    def test_phone_rename_preserves_untracked_media_and_refuses_existing_target_collision(self):
        item = self.photo()
        _, jobs = self.copy(item)
        old_folder = Path(jobs[0]["destination"])
        untracked = old_folder / "user-added.txt"
        untracked.write_bytes(b"user-added unchanged")
        occupied = old_folder.parent / "Occupied"
        occupied.mkdir()
        sentinel = occupied / "sentinel.txt"
        sentinel.write_bytes(b"unrelated destination")
        with self.assertRaises(UserError):
            self.engine.handle("rename_device", {"name": "Occupied"})
        self.assertIsNone(self.engine._devices[self.phone].get("alias"))
        self.assertEqual(untracked.read_bytes(), b"user-added unchanged")
        self.assertEqual(sentinel.read_bytes(), b"unrelated destination")
        self.engine.handle("rename_device", {"name": "New Name"})
        self.assertEqual((old_folder.parent / "New Name" / untracked.name).read_bytes(), b"user-added unchanged")
        self.assertEqual(sentinel.read_bytes(), b"unrelated destination")

    def test_phone_rename_rejects_active_transfer_and_reparse_source_without_mutation(self):
        item = self.photo()
        _, jobs = self.copy(item)
        original = Path(jobs[0]["localPath"])
        jobs[0]["state"] = "transferring"
        self.engine._active_job = jobs[0]["id"]
        with self.assertRaises(UserError):
            self.engine.handle("rename_device", {"name": "Busy"})
        jobs[0]["state"] = "completed"
        self.engine._active_job = None
        with mock.patch.object(self.engine, "_plain_directory_path", return_value=False):
            with self.assertRaises(UserError):
                self.engine.handle("rename_device", {"name": "Linked"})
        self.assertIsNone(self.engine._devices[self.phone].get("alias"))
        self.assertTrue(original.is_file())

    def test_phone_rename_save_failure_rolls_back_disk_metadata_and_history(self):
        item = self.photo()
        _, jobs = self.copy(item)
        original = Path(jobs[0]["localPath"])
        with mock.patch.object(self.engine, "_save_locked", side_effect=PermissionError("synthetic cache lock")):
            with self.assertRaises(PermissionError):
                self.engine.handle("rename_device", {"name": "Unsaved name"})
        self.assertTrue(original.is_file())
        self.assertIsNone(self.engine._devices[self.phone].get("alias"))
        self.assertEqual(self.engine.state()["catalogue"][0]["localPath"], str(original))
        self.assertEqual(self.engine._location_history, [])
        self.assertFalse(self.engine.folder_rename_file.exists())

    def test_interrupted_phone_rename_recovers_exact_folder_paths_and_history_on_startup(self):
        item = self.photo()
        _, jobs = self.copy(item)
        original = Path(jobs[0]["localPath"])
        self.engine.save()
        with mock.patch.object(self.engine, "_save_locked", side_effect=KeyboardInterrupt("synthetic process interruption")):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.handle("rename_device", {"name": "Recovered Name"})
        self.assertTrue(self.engine.folder_rename_file.is_file())
        restored = Engine(self.engine.data_dir, start=False, download_dir=self.engine.download_root)
        try:
            renamed = original.parent.parent / "Recovered Name" / original.name
            self.assertEqual(restored._devices[self.phone]["alias"], "Recovered Name")
            self.assertEqual(restored.state()["catalogue"][0]["localPath"], str(renamed))
            self.assertIn(str(renamed), restored._download_records)
            self.assertTrue(restored.state()["locationHistory"]["canUndo"])
            self.assertFalse(restored.folder_rename_file.exists())
            restored.handle("undo_download_location", {})
            self.assertEqual(restored.state()["catalogue"][0]["localPath"], str(original))
        finally:
            restored.close()
            # The interrupted process must not overwrite the recovered profile.
            self.engine._devices = copy.deepcopy(restored._devices)
            self.engine._catalogues = copy.deepcopy(restored._catalogues)
            self.engine._jobs = copy.deepcopy(restored._jobs)
            self.engine._download_records = copy.deepcopy(restored._download_records)
            self.engine._location_history = copy.deepcopy(restored._location_history)
            self.engine._location_cursor = restored._location_cursor

    def test_case_only_phone_rename_retains_original_identity_and_pc_tag(self):
        item = self.photo()
        _, jobs = self.copy(item)
        before = self.engine._file_identity(Path(jobs[0]["localPath"]))
        self.engine.handle("rename_device", {"name": "SAMPLE DEVICE"})
        path = Path(self.engine.state()["catalogue"][0]["localPath"])
        self.assertEqual(path.parent.name, "SAMPLE DEVICE")
        self.assertEqual(self.engine._file_identity(path), before)

    def test_invalid_windows_characters_are_sanitized_but_valid_spaces_and_case_are_kept(self):
        self.engine.handle("rename_device", {"name": "My iPhone: Pro/Max?"})
        self.assertEqual(Path(self.engine.state()["downloadDir"]).name, "My iPhone_ Pro_Max_")
        self.assertEqual(self.engine._devices[self.phone]["alias"], "My iPhone: Pro/Max?")

    def test_global_folder_assigns_distinct_children_for_same_named_phones(self):
        item = self.photo()
        _, jobs = self.copy(item)
        old = Path(jobs[0]["localPath"])
        other = device_key("synthetic-same-name-second-phone")
        self.engine._devices[other] = {"id": other, "name": "Sample Device", "online": False}
        self.engine.state()
        result = self.engine.handle("set_default_download_dir", {"path": str(self.root / "separate-parent")})
        target = Path(result["rootPath"])
        self.assertEqual(Path(self.engine._devices[self.phone]["defaultDownloadDir"]), target / "Sample Device")
        self.assertEqual(Path(self.engine._devices[other]["defaultDownloadDir"]), target / "Sample Device (2)")
        self.assertEqual(old.read_bytes(), b"synthetic original")
        self.assertFalse((target / "Sample Device" / old.name).exists())

    def _run_after_mutation_unlock(self, command, args):
        started, finished = threading.Event(), threading.Event()
        results, errors = [], []
        def execute():
            started.set()
            try:
                results.append(self.engine.handle(command, args))
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        self.engine._destructive_lock.acquire()
        worker = threading.Thread(target=execute, daemon=True)
        try:
            worker.start()
            self.assertTrue(started.wait(1))
            # A cleanup callback needs the state lock while it owns the mutation
            # lock. A queued rename/undo must not take that lock while waiting.
            available = self.engine.lock.acquire(timeout=0.5)
            self.assertTrue(available, "Mutation must be acquired before the state lock.")
            if available:
                self.engine.lock.release()
            self.assertFalse(finished.is_set(), "The action must wait until cleanup releases the mutation lock.")
        finally:
            self.engine._destructive_lock.release()
            worker.join(2)
        self.assertTrue(finished.is_set(), "The queued action must complete after mutation unlock.")
        self.assertEqual(errors, [])
        return results[0]

    def test_explicit_phone_rename_and_folder_undo_redo_wait_outside_state_lock(self):
        item = self.photo()
        self.copy(item)
        rename = self._run_after_mutation_unlock("rename_device", {"name": "Serialized Phone"})
        self.assertEqual(self.engine._devices[self.phone]["alias"], "Serialized Phone")
        self._run_after_mutation_unlock("undo_download_location", {"historyId": rename["historyId"]})
        self.assertIsNone(self.engine._devices[self.phone].get("alias"))
        self._run_after_mutation_unlock("redo_download_location", {"historyId": rename["historyId"]})
        self.assertEqual(self.engine._devices[self.phone]["alias"], "Serialized Phone")

    def test_background_name_change_defers_nonblocking_while_cleanup_owns_mutation_lock(self):
        item = self.photo()
        _, jobs = self.copy(item)
        old = Path(jobs[0]["localPath"])
        self.engine._devices[self.phone]["name"] = "System Renamed Phone"
        self.engine._destructive_lock.acquire()
        try:
            started = time.monotonic()
            state = self.engine.state()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(Path(state["devices"][0]["defaultDownloadDir"]), old.parent)
            self.assertIn("operation finishes", state["devices"][0]["downloadFolderWarning"])
            self.assertTrue(old.is_file())
        finally:
            self.engine._destructive_lock.release()
        state = self.engine.state()
        current = old.parent.parent / "System Renamed Phone" / old.name
        self.assertTrue(current.is_file())
        self.assertEqual(state["catalogue"][0]["localPath"], str(current))
        self.assertNotIn("downloadFolderWarning", state["devices"][0])


if __name__ == "__main__":
    unittest.main()
