import contextlib
import copy
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from backend.engine import Engine, UserError, device_key
from backend.test_phone_folders import FolderAFC


class FolderEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.afc = FolderAFC()
        self.afc.directories.update({"/Documents/a", "/Documents/a/sub", "/Documents/b"})
        self.afc.add("/Documents/a/sub/clip.mov", b"one")
        self.afc.add("/Documents/b/keep.txt", b"two")
        @contextlib.contextmanager
        def connector(device, source, app):
            self.assertEqual(source, "app")
            yield self.afc
        self.engine = Engine(self.root / "data", start=False, connector=connector, download_dir=self.root / "Videos")
        self.phone = device_key("folder-fixture")
        self.app = "test.folders"
        self.engine._devices[self.phone] = {"id": self.phone, "name": "Phone", "online": True}
        self.engine._apps[self.phone] = [{"id": self.app, "name": "Documents"}]
        self.engine.selected_device = self.phone
        self.scope = self.engine.scope(self.phone, "app", self.app, "/Documents")
        self.engine.current_scope = self.scope
        self.engine._catalogues[self.scope] = []
        self.items = {}
        for path in ("/Documents/a", "/Documents/a/sub", "/Documents/a/sub/clip.mov", "/Documents/b"):
            item = self.engine._catalogue_item(self.phone, "app", self.app, path, self.afc.stat(path))
            self.items[path] = item
            self.engine._catalogues[self.scope].append(item)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def plan(self, *paths):
        return self.engine.handle("preview_phone_folder_delete", {"items": [self.items[p] for p in paths]})

    def test_confirmed_nested_folder_keeps_pc_copy_and_prunes_descendants(self):
        pc = self.root / "local.mov"
        pc.write_bytes(b"one")
        plan = self.plan("/Documents/a")
        self.assertNotIn("signature", plan["files"][0])
        self.assertIn("Undo", plan["warning"])
        result = self.engine.handle("confirm_phone_folder_delete", {"token": plan["token"]})
        self.assertEqual((result["deleted"], result["failed"]), (1, 0))
        self.assertEqual(pc.read_bytes(), b"one")
        self.assertEqual([i["path"] for i in self.engine._catalogues[self.scope]], ["/Documents/b"])
        self.assertTrue(self.afc.exists("/Documents/b/keep.txt"))
        with self.assertRaises(UserError):
            self.engine.handle("confirm_phone_folder_delete", {"token": plan["token"]})

    def test_changed_second_folder_keeps_entire_selection(self):
        plan = self.plan("/Documents/a", "/Documents/b")
        self.afc.add("/Documents/b/new.txt", b"new")
        with self.assertRaises(UserError):
            self.engine.handle("confirm_phone_folder_delete", {"token": plan["token"]})
        self.assertFalse(self.afc.removed)

    def test_overlapping_folder_selection_collapses(self):
        plan = self.plan("/Documents/a", "/Documents/a/sub", "/Documents/a/sub/clip.mov")
        self.assertEqual(plan["fileCount"], 1)
        result = self.engine.handle("confirm_phone_folder_delete", {"token": plan["token"]})
        self.assertEqual(result["deleted"], 1)

    def test_active_child_download_blocks_folder_deletion(self):
        self.engine._jobs.append({"deviceId": self.phone, "state": "queued", "item": self.items["/Documents/a/sub/clip.mov"]})
        with self.assertRaisesRegex(UserError, "transfers"):
            self.plan("/Documents/a")

    def test_photos_and_documents_root_rejected(self):
        root = self.engine._catalogue_item(self.phone, "app", self.app, "/Documents", self.afc.stat("/Documents"))
        self.engine._catalogues[self.scope].append(root)
        with self.assertRaises(UserError):
            self.engine.handle("preview_phone_folder_delete", {"items": [root]})
        photo = copy.deepcopy(self.items["/Documents/a"])
        photo.update(id="photo", source="photos", appId=None, path="/DCIM")
        self.engine._catalogues[self.scope].append(photo)
        with self.assertRaises(UserError):
            self.engine.handle("preview_phone_folder_delete", {"items": [photo]})

    def test_recycled_without_receipt_reports_physical_success_and_warning(self):
        item = self.items["/Documents/a/sub/clip.mov"]
        self.engine.enqueue_download(self.phone, [item])
        self.engine.run_job(self.engine._jobs[-1])
        plan = self.engine.handle("preview_pc_cleanup", {"deviceId": self.phone})
        self.assertEqual(plan["fileCount"], 1)
        def recycled(path, validate):
            self.assertTrue(validate())
            path.unlink()
            return {"undoUnavailable": True, "recycled": True}
        with mock.patch.object(self.engine, "_remove_pc_copy", side_effect=recycled):
            result = self.engine.handle("confirm_pc_cleanup", {"token": plan["token"]})
        self.assertEqual((result["deleted"], result["failed"]), (1, 0))
        self.assertTrue(result["results"][0]["deleted"])
        self.assertIn("manually", result["receiptWarning"])
        self.assertIsNone(result["undoToken"])


if __name__ == "__main__":
    unittest.main()
