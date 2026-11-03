"""Opt-in real Windows recovery of new synthetic files only; no GUI/phone."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.pc_recycle import recycle_file, repair_recycle_receipt, restore_recycled_file
from backend.recycle_undo import RecycleUndoHistory

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = b"EyeCopy newly created native recovery fixture\n" * 20


@unittest.skipUnless(os.name == "nt" and os.environ.get("EYECOPY_RECYCLE_NATIVE_TESTS") == "1", "Opt-in new Windows Recycle Bin fixtures")
class NativeRecycleUndoTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "test-output" / "recycle-undo-windows"
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=base)
        self.directory = Path(self.temporary.name).resolve()
        self.assertIn(ROOT, self.directory.parents)
        self.receipts = []
        self.engine = None

    def tearDown(self):
        if self.engine is not None:
            self.engine.close()
        for value in reversed(self.receipts):
            original = Path(value["originalPath"])
            self.assertIn(self.directory, original.parents)
            if Path(value["binPath"]).exists() and not original.exists():
                restore_recycled_file(repair_recycle_receipt(value))
        # The entire resolved target is a new disposable directory inside the project.
        self.assertIn(ROOT, self.directory.resolve().parents)
        self.temporary.cleanup()

    def source(self, name="original.mov", parent=None):
        path = (parent or self.directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.assertIn(self.directory, path.parents)
        path.write_bytes(PAYLOAD)
        return path

    def capture(self, path):
        value = recycle_file(path, lambda: path.read_bytes() == PAYLOAD)
        self.assertFalse(value.get("capturePending"))
        self.receipts.append(value)
        return value

    def test_real_round_trip_and_restart(self):
        path = self.source(); value = self.capture(path)
        self.assertFalse(path.exists()); self.assertTrue(Path(value["binPath"]).is_file())
        value["originalRecord"] = {"deviceId": "fixture-phone", "path": str(path)}
        token = RecycleUndoHistory(self.directory / "data").record([value], "Native fixture cleanup")["undoToken"]
        restored = RecycleUndoHistory(self.directory / "data").undo(token)
        self.assertTrue(restored["complete"]); self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertFalse(Path(value["binPath"]).exists()); self.assertFalse(Path(value["infoPath"]).exists())
        history = RecycleUndoHistory(self.directory / "data")
        redone = history.redo(restored["redoToken"])
        self.assertTrue(redone["complete"]); self.assertFalse(path.exists())
        self.receipts.append(copy.deepcopy(history.entries[-1]["files"][0]["receipt"]))
        self.assertTrue(RecycleUndoHistory(self.directory / "data").undo(redone["undoToken"])["complete"])
        self.assertEqual(path.read_bytes(), PAYLOAD)

    def test_existing_and_last_moment_collisions_never_overwrite(self):
        path = self.source(); value = self.capture(path)
        collision = b"New conflicting synthetic file must survive"
        path.write_bytes(collision)
        with self.assertRaises(FileExistsError): restore_recycled_file(value)
        self.assertEqual(path.read_bytes(), collision); self.assertTrue(Path(value["binPath"]).exists())
        path.unlink()  # Exact conflict created above, never a user file.
        def race():
            path.write_bytes(collision)
            return True
        with self.assertRaises(OSError): restore_recycled_file(value, race)
        self.assertEqual(path.read_bytes(), collision); self.assertTrue(Path(value["binPath"]).exists())
        self.assertTrue(Path(value["infoPath"]).exists())
        path.unlink()
        restore_recycled_file(value)
        self.assertEqual(path.read_bytes(), PAYLOAD)

    def test_redo_vetoes_changed_restored_file(self):
        path = self.source(); value = self.capture(path)
        history = RecycleUndoHistory(self.directory / "data")
        restored = history.undo(history.record([value], "Changed fixture")["undoToken"])
        self.assertTrue(restored["complete"])
        changed = b"Edited synthetic original after Undo"
        path.write_bytes(changed)
        result = history.redo(restored["redoToken"])
        self.assertFalse(result["complete"]); self.assertEqual(result["deleted"], 0)
        self.assertEqual(path.read_bytes(), changed)

    def test_wrong_metadata_digest_keeps_bin_file_and_metadata(self):
        path = self.source(); value = self.capture(path)
        bad = copy.deepcopy(value); bad["infoDigest"] = "0" * 64
        with self.assertRaises(OSError): restore_recycled_file(bad)
        self.assertFalse(path.exists()); self.assertTrue(Path(value["binPath"]).exists())
        self.assertTrue(Path(value["infoPath"]).exists())
        restore_recycled_file(value)

    def test_capture_failure_reports_recycled_and_repairs_exact_receipt(self):
        path = self.source()
        with mock.patch("backend.pc_recycle._capture_receipt", side_effect=PermissionError("Temporary lock")):
            value = recycle_file(path, lambda: path.read_bytes() == PAYLOAD)
        self.receipts.append(value)
        self.assertTrue(value["recycled"]); self.assertTrue(value["capturePending"])
        self.assertFalse(path.exists()); self.assertTrue(Path(value["binPath"]).exists())
        history = RecycleUndoHistory(self.directory / "data")
        result = history.undo(history.record([value], "Pending fixture")["undoToken"])
        self.assertTrue(result["complete"]); self.assertEqual(path.read_bytes(), PAYLOAD)

    def test_actual_engine_cleanup_undo_redo_restart_restores_tags(self):
        from backend.engine import Engine, device_key
        data, downloads = self.directory / "engine-data", self.directory / "engine-downloads"
        self.engine = Engine(data, start=False, download_dir=downloads)
        phone = device_key("native-no-phone-fixture")
        self.engine._devices[phone] = {"id": phone, "name": "Synthetic EyeCopy Phone", "online": False}
        self.engine.selected_device = phone
        self.engine._ensure_device_directories_locked()
        path = self.source(parent=Path(self.engine._devices[phone]["defaultDownloadDir"]))
        item = {"id": "native-fixture-item", "deviceId": phone, "source": "photos", "appId": None,
                "path": "/DCIM/original.mov", "name": path.name, "kind": "video", "size": len(PAYLOAD), "modified": "fixture-source-time"}
        scope = self.engine.scope(phone, "photos", None, "/DCIM")
        self.engine._catalogues[scope] = [item]; self.engine.current_scope = scope
        self.engine._download_records[str(path)] = {"deviceId": phone, "itemId": item["id"], "path": str(path),
            "identity": self.engine._file_identity(path), "modifiedNs": path.stat().st_mtime_ns,
            "sourceSignature": {"size": item["size"], "modified": item["modified"]}}
        self.engine._reconcile_default_copies_locked(); self.engine._save_locked()
        self.assertEqual(item.get("localPath"), str(path))
        cleanup = self.engine.confirm_pc_cleanup(self.engine.preview_pc_cleanup(phone)["token"])
        self.assertEqual(cleanup["deleted"], 1); self.assertTrue(cleanup["undoToken"])
        self.receipts.append(copy.deepcopy(self.engine._pc_history.entries[-1]["files"][0]["receipt"]))
        self.assertFalse(path.exists()); self.assertNotIn("localPath", item)
        self.engine.close(); self.engine = Engine(data, start=False, download_dir=downloads)
        restored = self.engine.change_pc_cleanup_history(True, cleanup["undoToken"])
        self.assertTrue(restored["complete"]); self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertNotIn("originalRecord", restored["results"][0])
        self.assertEqual(self.engine._catalogues[scope][0].get("localPath"), str(path))
        self.engine.close(); self.engine = Engine(data, start=False, download_dir=downloads)
        self.assertEqual(self.engine._catalogues[scope][0].get("localPath"), str(path))
        redone = self.engine.change_pc_cleanup_history(False, restored["redoToken"])
        self.assertTrue(redone["complete"]); self.assertFalse(path.exists())
        self.receipts.append(copy.deepcopy(self.engine._pc_history.entries[-1]["files"][0]["receipt"]))
        self.assertNotIn("localPath", self.engine._catalogues[scope][0])
        self.assertTrue(self.engine.change_pc_cleanup_history(True, redone["undoToken"])["complete"])
        self.assertEqual(path.read_bytes(), PAYLOAD)
        report = {"passed": True, "actualWindowsRecycleUndoRedo": True, "actualEngineRestart": True,
                  "defaultCopyTagRestored": True, "exactBinReceiptsOnly": True, "overwriteAllowed": False,
                  "phoneAccess": False, "existingUserFilesTouched": False, "frozenV1Touched": False}
        (ROOT / "test-output" / "recycle-undo-windows-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
