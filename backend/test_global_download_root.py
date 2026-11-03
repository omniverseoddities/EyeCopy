"""App-wide default folder preferences never move existing PC media."""
import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from backend.engine import Engine, UserError, device_key
from backend import test_download_library as library_fixtures


class GlobalRootTests(unittest.TestCase):
    setUp = library_fixtures.DownloadLibraryTests.setUp
    tearDown = library_fixtures.DownloadLibraryTests.tearDown
    photo = library_fixtures.DownloadLibraryTests.photo
    copy = library_fixtures.DownloadLibraryTests.copy
    restore = library_fixtures.DownloadLibraryTests.restore

    def test_all_current_and_future_phones_use_one_root_and_old_files_stay_put(self):
        _, jobs = self.copy(self.photo())
        old = self.engine.download_root
        original = Path(jobs[0]["localPath"])
        other = device_key("synthetic-global-second-phone")
        self.engine._devices[other] = {"id": other, "name": "Other Phone", "online": False}
        self.engine.state()
        folder = Path(self.engine._devices[other]["defaultDownloadDir"])
        folder.mkdir()
        (folder / "other.dat").write_bytes(b"synthetic second phone")
        legacy = old / "iphone-1"
        legacy.mkdir()
        (legacy / "legacy.dat").write_bytes(b"synthetic legacy contents")
        (old / "untracked.txt").write_bytes(b"synthetic untracked contents")
        before = {str(path.relative_to(old)): path.read_bytes() for path in old.rglob("*") if path.is_file()}
        old_records = copy.deepcopy(self.engine._download_records)
        result = self.engine.handle("set_default_download_dir", {"path": str(self.root / "chosen-parent")})
        new = self.root / "chosen-parent" / "EyeCopy"
        self.assertEqual(result["rootPath"], str(new))
        self.assertEqual(self.engine.state()["defaultBaseDir"], str(new.parent))
        self.assertEqual({str(path.relative_to(old)): path.read_bytes() for path in old.rglob("*") if path.is_file()}, before)
        self.assertFalse(new.exists())
        self.assertTrue(all(Path(device["defaultDownloadDir"]).parent == new for device in self.engine._devices.values()))
        self.assertEqual(self.engine._download_records, old_records)
        self.assertEqual(jobs[0]["localPath"], str(original))
        self.assertNotIn("localPath", self.engine.state()["catalogue"][0])
        future = device_key("synthetic-future-phone")
        self.engine._devices[future] = {"id": future, "name": "Future Phone", "online": False}
        self.engine.state()
        self.assertEqual(self.engine._devices[future]["defaultDownloadDir"], str(new / "Future Phone"))

    def test_global_undo_redo_preferences_only_cover_a_phone_added_later(self):
        _, jobs = self.copy(self.photo())
        old = self.engine.download_root
        original = Path(jobs[0]["localPath"])
        result = self.engine.handle("set_default_download_dir", {"path": str(self.root / "chosen")})
        future = device_key("synthetic-future-for-undo")
        self.engine._devices[future] = {"id": future, "name": "Future Phone", "online": False}
        self.engine.state()
        self.engine.handle("undo_download_location", {"historyId": result["historyId"]})
        self.assertEqual(self.engine.download_root, old)
        self.assertEqual(self.engine._devices[future]["defaultDownloadDir"], str(old / "Future Phone"))
        self.assertEqual(self.engine.state()["catalogue"][0]["localPath"], str(original))
        self.engine.handle("redo_download_location", {"historyId": result["historyId"]})
        self.assertEqual(self.engine._devices[future]["defaultDownloadDir"], str(self.root / "chosen" / "EyeCopy" / "Future Phone"))
        self.assertTrue(original.is_file())
        self.assertFalse((self.root / "chosen" / "EyeCopy").exists())

    def test_no_phone_is_needed_and_app_global_root_persists(self):
        self.engine._devices.clear()
        self.engine.selected_device = None
        self.engine.current_scope = None
        parent = self.root / "global-parent"
        self.engine.handle("set_default_download_dir", {"path": str(parent)})
        restored = self.restore()
        try:
            self.assertEqual(restored.state()["downloadRoot"], str(parent / "EyeCopy"))
            self.assertEqual(restored.state()["defaultBaseDir"], str(parent))
        finally:
            restored.close()

    def test_existing_destination_contents_are_preserved_and_nested_locations_are_allowed(self):
        self.copy(self.photo())
        existing = self.root / "existing" / "EyeCopy"
        existing.mkdir(parents=True)
        sentinel = existing / "untracked.txt"
        sentinel.write_bytes(b"existing unrelated media")
        self.engine.handle("set_default_download_dir", {"path": str(existing.parent)})
        self.assertEqual(sentinel.read_bytes(), b"existing unrelated media")
        self.engine.handle("set_default_download_dir", {"path": str(existing / "nested-parent")})
        self.assertEqual(self.engine.download_root, existing / "nested-parent" / "EyeCopy")
        self.assertEqual(sentinel.read_bytes(), b"existing unrelated media")

    def test_data_cache_existing_file_and_link_destinations_are_rejected_without_changes(self):
        _, jobs = self.copy(self.photo())
        original = Path(jobs[0]["localPath"])
        for parent in (self.engine.data_dir, self.engine.cache_dir, self.engine.cache_dir / "nested"):
            with self.subTest(parent=str(parent)), self.assertRaises(UserError):
                self.engine.handle("set_default_download_dir", {"path": str(parent)})
            self.assertEqual(original.read_bytes(), b"synthetic original")
        parent = self.root / "occupied"
        parent.mkdir()
        (parent / "EyeCopy").write_bytes(b"existing ordinary file")
        with self.assertRaises(UserError):
            self.engine.handle("set_default_download_dir", {"path": str(parent)})
        with mock.patch.object(self.engine, "_plain_directory_path", return_value=False), self.assertRaises(UserError):
            self.engine.handle("set_default_download_dir", {"path": str(self.root / "linked")})
        self.assertEqual(original.read_bytes(), b"synthetic original")

    def test_queued_jobs_and_complete_partials_keep_original_destinations(self):
        item = self.photo(contents=b"complete synthetic partial")
        self.engine.enqueue_download(self.phone, [item])
        job = self.engine._jobs[-1]
        with mock.patch.object(self.engine, "_publish_download", side_effect=PermissionError("synthetic lock")):
            self.engine.run_job(job)
        before = copy.deepcopy({key: job.get(key) for key in ("destination", "partPath", "finalPath", "publicationIdentity", "sourceSignature")})
        part = Path(job["partPath"])
        before_reads = len(self.afc.read_calls)
        queued = self.photo("still-queued.JPG")
        self.engine.enqueue_download(self.phone, [queued])
        queued_destination = self.engine._jobs[-1]["destination"]
        self.engine.handle("set_default_download_dir", {"path": str(self.root / "future-default")})
        self.assertEqual({key: job.get(key) for key in before}, before)
        self.assertEqual(self.engine._jobs[-1]["destination"], queued_destination)
        self.assertEqual(part.read_bytes(), b"complete synthetic partial")
        self.engine.control_job(job["id"], "retry")
        self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(len(self.afc.read_calls), before_reads)
        self.assertNotIn("localPath", self.engine.state()["catalogue"][0])

    def test_default_root_without_saved_preference_does_not_move_any_media(self):
        _, jobs = self.copy(self.photo())
        old = self.engine.download_root
        original = Path(jobs[0]["localPath"])
        legacy = old / "iphone-1"
        legacy.mkdir()
        (legacy / "old.dat").write_bytes(b"preserved legacy")
        self.engine.save()
        data = json.loads(self.engine.state_file.read_text())
        data.pop("downloadRootDir", None)
        data.pop("downloadLayoutVersion", None)
        self.engine.state_file.write_text(json.dumps(data))
        desired = old.parent / "EyeCopy"
        restored = Engine(self.engine.data_dir, start=False, download_dir=desired)
        try:
            self.assertEqual(restored.download_root, desired)
            self.assertEqual(original.read_bytes(), b"synthetic original")
            self.assertEqual((legacy / "old.dat").read_bytes(), b"preserved legacy")
            self.assertFalse(desired.exists())
            self.assertEqual(restored._devices[self.phone]["defaultDownloadDir"], str(desired / "Sample Device"))
            self.assertNotIn("localPath", restored.state()["catalogue"][0])
        finally:
            restored.close()
            for key in ("_devices", "_catalogues", "_jobs", "_download_records", "_location_history", "_location_cursor", "download_root"):
                setattr(self.engine, key, copy.deepcopy(getattr(restored, key)))


if __name__ == "__main__":
    unittest.main()
