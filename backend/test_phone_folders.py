"""Deletion invariants use in-memory app documents, never a real phone."""
import unittest
from backend.phone_folders import snapshot, validate, remove, FolderChangedError
from backend.test_backend import FakeAFC


class FolderAFC(FakeAFC):
    def __init__(self):
        super().__init__()
        self.removed = []
        self.before_remove = None

    def rm_single(self, path):
        if self.before_remove:
            self.before_remove(path)
        if path in self.directories:
            if len(self.listdir(path)) > 2:
                raise OSError("Directory not empty")
            self.directories.remove(path)
        else:
            self.files.pop(path)
        self.removed.append(path)


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.afc = FolderAFC()
        self.afc.directories.update({"/Documents/parent", "/Documents/parent/child"})
        self.afc.add("/Documents/parent/a.txt", b"a")
        self.afc.add("/Documents/parent/child/b.txt", b"bb")
        self.afc.add("/Documents/keep.txt", b"outside")

    def test_nested_folder_removes_only_approved_contents(self):
        plan = snapshot(self.afc, "/Documents/parent")
        self.assertEqual((plan["fileCount"], plan["folderCount"], plan["bytes"]), (2, 2, 3))
        result = remove(self.afc, plan)
        self.assertTrue(result["deleted"])
        self.assertEqual(self.afc.removed[-1], "/Documents/parent")
        self.assertTrue(self.afc.exists("/Documents/keep.txt"))

    def test_root_traversal_and_external_paths_rejected(self):
        for path in ("/Documents", "/Documents/../DCIM", "/DCIM/test", "/Documents/a\\b"):
            with self.assertRaises(ValueError):
                snapshot(self.afc, path)
        self.assertFalse(self.afc.removed)

    def test_symlink_in_tree_or_ancestor_rejects_without_removal(self):
        self.afc.links.add("/Documents/parent/link")
        with self.assertRaises(ValueError):
            snapshot(self.afc, "/Documents/parent")
        self.afc.links.clear()
        self.afc.links.add("/Documents/parent")
        with self.assertRaises(ValueError):
            snapshot(self.afc, "/Documents/parent/child")

    def test_added_or_changed_file_invalidates_warning_before_delete(self):
        plan = snapshot(self.afc, "/Documents/parent")
        self.afc.add("/Documents/parent/new.txt", b"new")
        with self.assertRaises(FolderChangedError):
            validate(self.afc, plan)
        with self.assertRaises(FolderChangedError):
            remove(self.afc, plan)
        self.assertFalse(self.afc.removed)

    def test_new_data_mid_delete_is_kept(self):
        plan = snapshot(self.afc, "/Documents/parent")
        def inject(path):
            if path.endswith("a.txt"):
                self.afc.add("/Documents/parent/new.txt", b"new")
        self.afc.before_remove = inject
        result = remove(self.afc, plan)
        self.assertFalse(result["deleted"])
        self.assertTrue(self.afc.exists("/Documents/parent/new.txt"))
        self.assertTrue(self.afc.exists("/Documents/parent"))

    def test_entry_and_depth_limits_keep_tree(self):
        for kwargs in ({"max_entries": 2}, {"max_depth": 0}):
            with self.assertRaises(ValueError):
                snapshot(self.afc, "/Documents/parent", **kwargs)
        self.assertFalse(self.afc.removed)

    def test_selected_regular_file_is_bounded_for_mixed_batches(self):
        plan = snapshot(self.afc, "/Documents/keep.txt")
        self.assertEqual(plan["folderCount"], 0)
        self.assertTrue(remove(self.afc, plan)["deleted"])
        self.assertTrue(self.afc.exists("/Documents/parent/a.txt"))


if __name__ == "__main__":
    unittest.main()
