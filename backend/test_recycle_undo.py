"""History durability/order/partial-failure tests; never touch the real bin."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.recycle_undo import RecycleUndoHistory

ROOT = Path(__file__).resolve().parents[1]


def receipt(index=1):
    identity = {"volume": 1, "fileId": index, "size": 16, "modified": 123}
    return {"version": 1, "id": str(index), "originalPath": "C:\\EyeCopy-fixtures\\original-%s.mov" % index,
            "binPath": "C:\\$Recycle.Bin\\S-1-5-21-1000\\$Rfixture%s.mov" % index,
            "infoPath": "C:\\$Recycle.Bin\\S-1-5-21-1000\\$Ifixture%s.mov" % index,
            "originalIdentity": identity, "binIdentity": identity,
            "parentIdentity": {"volume": 1, "fileId": 999},
            "infoIdentity": {"volume": 1, "fileId": 1000 + index, "size": 80, "modified": 123},
            "infoDigest": "0" * 64, "originalRecord": {"deviceId": "fixture-phone", "itemId": str(index)}}


class RecycleHistoryTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "test-output" / "recycle-undo-unit"
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=base)
        self.directory = Path(self.temporary.name).resolve()
        self.assertIn(ROOT, self.directory.parents)
        self.restores, self.recycles, self.fail_paths = [], [], set()
        self.identities = {}

    def tearDown(self):
        self.assertIn(ROOT, self.directory.resolve().parents)
        self.temporary.cleanup()

    def restore(self, value, validate):
        if value["originalPath"] in self.fail_paths:
            raise FileExistsError("A new file occupies the original location.")
        self.assertTrue(validate())
        self.restores.append(value["id"])
        self.identities[value["originalPath"]] = copy.deepcopy(value["binIdentity"])
        return {"restoredIdentity": value["binIdentity"], "infoCleanupPending": False}

    def recycle(self, path, validate):
        self.assertTrue(validate())
        self.recycles.append(str(path))
        old = next(index for index in range(1, 10) if receipt(index)["originalPath"] == str(path))
        value = receipt(old); value["id"] += "-redone"
        return value

    def history(self):
        def identity(path):
            if str(path) not in self.identities:
                raise FileNotFoundError(str(path))
            return self.identities[str(path)]
        return RecycleUndoHistory(self.directory, recycler=self.recycle, restorer=self.restore,
                                  identity=identity, cleanup=lambda value: True)

    def test_round_trip_persists_original_metadata_and_tokens(self):
        history = self.history(); action = history.record([receipt()], "Remove fixture copy")
        self.assertTrue(history.state()["undoAt"])
        result = self.history().undo(action["undoToken"])
        self.assertTrue(result["complete"]); self.assertEqual(result["restored"], 1)
        self.assertEqual(result["results"][0]["originalRecord"]["deviceId"], "fixture-phone")
        after = self.history(); self.assertTrue(after.state()["canRedo"])
        self.assertEqual(len(after.restored_records()), 1)
        redone = after.redo(result["redoToken"])
        self.assertTrue(redone["complete"]); self.assertEqual(redone["deleted"], 1)
        self.assertEqual(self.history().undo(redone["undoToken"])["restored"], 1)

    def test_partial_undo_retries_only_the_failed_component(self):
        history = self.history(); token = history.record([receipt(1), receipt(2)], "Remove 2 copies")["undoToken"]
        self.fail_paths.add(receipt(2)["originalPath"])
        first = history.undo(token)
        self.assertFalse(first["complete"]); self.assertIsNone(first["redoToken"])
        self.assertEqual(self.restores, ["1"])
        self.fail_paths.clear()
        second = self.history().undo(token)
        self.assertTrue(second["complete"]); self.assertEqual(second["restored"], 1)
        self.assertEqual(self.restores, ["1", "2"])

    def test_redo_refuses_edited_or_replaced_restored_file(self):
        history = self.history(); token = history.record([receipt()], "Remove copy")["undoToken"]
        result = history.undo(token)
        self.identities[receipt()["originalPath"]]["modified"] += 1
        blocked = history.redo(result["redoToken"])
        self.assertFalse(blocked["complete"]); self.assertEqual(blocked["deleted"], 0)
        self.assertEqual(self.recycles, [])

    def test_token_order_and_new_actions_invalidate_redo(self):
        history = self.history(); first = history.record([receipt(1)], "First")["undoToken"]
        second = history.record([receipt(2)], "Second")["undoToken"]
        with self.assertRaises(ValueError): history.undo(first)
        history.undo(second); history.record([receipt(3)], "Third")
        self.assertFalse(history.state()["canRedo"])

    def test_bulk_append_is_durable_per_file_and_one_ordered_action(self):
        history = self.history(); token = history.record([receipt(1)], "One bulk cleanup")["undoToken"]
        appended = history.append(token, [receipt(2)])
        self.assertEqual(appended["undoToken"], token)
        restart = self.history()
        self.assertEqual(len(restart.entries), 1)
        self.assertEqual(len(restart.entries[0]["files"]), 2)
        restart.append(token, [receipt(2)])
        self.assertEqual(len(restart.entries[0]["files"]), 2, "A duplicate completed receipt cannot be undone twice")
        self.assertEqual(restart.undo(token)["restored"], 2)
        with self.assertRaises(ValueError): restart.append(token, [receipt(3)])

    def test_blocked_main_history_retains_durable_journal_after_restart(self):
        history = self.history()
        replace = os.replace
        def blocked_main(source, target):
            if Path(target) == history.path: raise PermissionError("Windows reader locks history")
            return replace(source, target)
        with mock.patch("backend.recycle_undo.os.replace", side_effect=blocked_main):
            action = history.record([receipt()], "Durable blocked-save copy")
            self.assertTrue(action["savePending"])
            self.assertTrue(history.journal.exists())
            self.assertTrue(history.state()["canUndo"])
        recovered = self.history()
        self.assertEqual(recovered.state()["undoToken"], action["undoToken"])
        self.assertFalse(recovered.state()["savePending"])
        self.assertTrue(recovered.undo(action["undoToken"])["complete"])

    def test_all_save_failures_keep_physical_receipt_in_memory_until_retry(self):
        history = self.history()
        with mock.patch.object(history, "_atomic_write", side_effect=PermissionError("blocked")):
            action = history.record([receipt()], "Recover when saving unblocks")
            self.assertTrue(action["savePending"])
            self.assertEqual(history.entries[0]["files"][0]["receipt"], receipt())
        self.assertFalse(history.state()["savePending"])
        self.assertEqual(self.history().state()["undoToken"], action["undoToken"])

    def test_corrupt_receipt_history_is_not_a_bin_search(self):
        directory = self.directory / "invalid"; directory.mkdir()
        (directory / "recycle-undo.json").write_text('{"version":1,"entries":[{"token":"x","files":[{"receipt":{"originalPath":"C:\\\\other"}}]}],"cursor":1}', encoding="utf-8")
        history = RecycleUndoHistory(directory)
        self.assertFalse(history.state()["canUndo"])
        self.assertIsNotNone(history.state()["historyWarning"])


if __name__ == "__main__":
    unittest.main()
