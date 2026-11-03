"""Real NTFS aliases must preserve default-only tracking and reparse guards."""
import contextlib
import ctypes
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.engine import Engine, UserError, device_key
from backend.test_backend import FakeAFC


@unittest.skipUnless(os.name == "nt", "Windows filesystem aliases")
class WindowsAliasTests(unittest.TestCase):
    def setUp(self):
        output = Path(__file__).resolve().parent.parent / "test-output"
        output.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="windows-alias-", dir=output)
        self.root = Path(self.temporary.name).resolve()
        self.long = self.root / "Long directory alias fixture"
        self.long.mkdir()
        short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        short_path.restype = ctypes.c_uint
        text = ctypes.create_unicode_buffer(32768)
        length = short_path(str(self.long), text, len(text))
        if not length or length >= len(text) or Path(text.value) == self.long:
            self.temporary.cleanup()
            self.skipTest("This volume does not generate distinct 8.3 aliases.")
        self.short = Path(text.value)
        self.afc = FakeAFC()
        self.phone = device_key("synthetic-alias-device")

        @contextlib.contextmanager
        def connector(*_args):
            yield self.afc

        self.engine = Engine(self.root / "data", start=False, connector=connector,
                             download_dir=self.root / "EyeCopy")
        self.engine._devices[self.phone] = {
            "id": self.phone, "name": "Alias Device", "online": True,
            "defaultDownloadDir": str(self.short / "Device originals"),
            "downloadDir": str(self.short / "Device originals"), "defaultDownloadKind": "custom",
        }
        self.engine.selected_device = self.phone
        self.engine.current_scope = self.engine.scope(self.phone, "photos")
        remote = "/DCIM/100APPLE/alias.JPG"
        self.afc.add(remote, b"synthetic alias original")
        self.item = self.engine._catalogue_item(self.phone, "photos", None, remote, self.afc.stat(remote))
        self.engine._catalogues[self.engine.current_scope] = [self.item]

    def tearDown(self):
        self.engine.close()
        self.temporary.cleanup()

    def test_alias_default_sync_marks_pc_skips_duplicates_and_survives_restart(self):
        result = self.engine.enqueue_download(self.phone, [self.item])
        job = next(job for job in self.engine._jobs if job["id"] in result["jobIds"])
        self.assertTrue(job["syncDownload"])
        self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        original = Path(job["localPath"])
        self.assertEqual(original.parent, self.long / "Device originals")
        self.assertEqual(self.engine.state()["catalogue"][0]["localPath"], str(original))
        duplicate = self.engine.enqueue_download(self.phone, [self.item])
        self.assertEqual(duplicate["jobIds"], [])
        self.assertEqual(duplicate["skippedItemIds"], [self.item["id"]])
        self.engine.save()
        restored = Engine(self.engine.data_dir, start=False, download_dir=self.engine.download_root)
        try:
            self.assertEqual(restored.state()["catalogue"][0]["localPath"], str(original))
            self.assertEqual(restored.preview_pc_cleanup(self.phone)["fileCount"], 1)
            original.unlink()
            self.assertNotIn("localPath", restored.state()["catalogue"][0])
        finally:
            restored.close()

    def test_alias_parent_changes_global_root_without_statting_old_or_external_records(self):
        self.engine.enqueue_download(self.phone, [self.item])
        job = self.engine._jobs[-1]
        self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        original = Path(job["localPath"])
        result = self.engine.handle("set_default_download_dir", {"path": str(self.short / "Future parent")})
        self.assertEqual(Path(result["rootPath"]), self.long / "Future parent" / "EyeCopy")
        outside = self.long / "Elsewhere" / "keep.JPG"
        self.engine._download_records[str(outside)] = {"deviceId": self.phone, "itemId": self.item["id"], "path": str(outside)}
        self.engine._devices[self.phone]["downloadDir"] = str(outside.parent)
        real_lstat = Path.lstat

        def guarded_lstat(path, *args, **kwargs):
            self.assertNotEqual(path, outside)
            self.assertNotEqual(path, outside.parent)
            self.assertNotEqual(path, original)
            self.assertNotEqual(path, original.parent)
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", autospec=True, side_effect=guarded_lstat):
            self.assertNotIn("localPath", self.engine.state()["catalogue"][0])
            self.assertEqual(self.engine.preview_pc_cleanup(self.phone)["fileCount"], 0)
        self.assertEqual(original.read_bytes(), b"synthetic alias original")

    def test_live_and_broken_junctions_are_rejected_before_resolving_aliases(self):
        target = self.long / "Preserved target"
        target.mkdir()
        sentinel = target / "keep.txt"
        sentinel.write_bytes(b"unrelated synthetic file")
        junction = self.long / "Linked parent"
        environment = {**os.environ, "EYECOPY_TEST_JUNCTION": str(junction), "EYECOPY_TEST_TARGET": str(target)}
        created = subprocess.run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "New-Item -ItemType Junction -Path $env:EYECOPY_TEST_JUNCTION -Target $env:EYECOPY_TEST_TARGET | Out-Null",
        ], env=environment, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(created.returncode, 0, created.stderr.decode(errors="replace"))
        alias_junction = self.short / junction.name
        before = self.engine.download_root
        try:
            for broken in (False, True):
                with self.subTest(broken=broken):
                    if broken:
                        kept = target.with_name("Moved preserved target")
                        target.rename(kept)
                    with self.assertRaises(UserError):
                        self.engine.handle("set_default_download_dir", {"path": str(alias_junction / "Future parent")})
                    self.assertEqual(self.engine.download_root, before)
                    current = kept / sentinel.name if broken else sentinel
                    self.assertEqual(current.read_bytes(), b"unrelated synthetic file")
        finally:
            # Remove only our junction entry, never recurse through its target.
            junction.rmdir()


if __name__ == "__main__":
    unittest.main()
