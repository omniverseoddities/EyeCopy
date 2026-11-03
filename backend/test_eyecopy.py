"""EyeCopy device folders and confirmed mutations, using only temporary fixtures."""
import contextlib
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.engine import Engine, SourceChangedError, UserError, device_key
from backend.test_backend import FakeAFC


class FilesAFC(FakeAFC):
    def __init__(self):
        super().__init__()
        self.removed = []
        self.created = []
        self.fail_remove = None

    def makedirs(self, path):
        if self.exists(path):
            raise FileExistsError(path)
        self.created.append(path)
        self.directories.add(path)

    def rm_single(self, path):
        if path == self.fail_remove:
            raise PermissionError(path)
        self.removed.append(path)
        self.files.pop(path)
        return True


class EyeCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.afc = FilesAFC()
        self.events = []
        self.phone = device_key("fixture-one")
        self.other = device_key("fixture-two")
        self.app = "test.sharing"

        @contextlib.contextmanager
        def connector(device, source, app):
            self.assertEqual(source, "app")
            self.assertIn(device, {self.phone, self.other})
            yield self.afc

        self.engine = Engine(self.root / "data", start=False, connector=connector,
                             download_dir=self.root / "Videos" / "EyeCopy",
                             emit=lambda event, payload: self.events.append((event, copy.deepcopy(payload))))
        self.engine._devices[self.phone] = {"id": self.phone, "name": "Zulu", "online": True}
        self.engine._devices[self.other] = {"id": self.other, "name": "Alpha", "online": False}
        self.engine.selected_device = self.phone
        self.engine._apps[self.phone] = [{"id": self.app, "name": "Documents", "filesVisible": True}]
        self.engine._apps[self.other] = [{"id": self.app, "name": "Documents"}]

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def item(self, name="clip.mov", contents=b"original"):
        path = "/Documents/" + name
        self.afc.add(path, contents)
        item = self.engine._catalogue_item(self.phone, "app", self.app, path, self.afc.stat(path))
        scope = self.engine.scope(self.phone, "app", self.app, "/Documents")
        self.engine.current_scope = scope
        self.engine._catalogues.setdefault(scope, []).append(item)
        return item

    def download(self, name="clip.mov", destination=None):
        item = self.item(name)
        self.engine.enqueue_download(self.phone, [item], destination)
        job = self.engine._jobs[-1]
        self.engine.run_job(job)
        self.assertEqual(job["state"], "completed")
        return job

    def test_device_defaults_use_stable_phone_names_and_are_persisted(self):
        state = self.engine.state()
        devices = {item["id"]: item for item in state["devices"]}
        self.assertEqual(Path(devices[self.phone]["defaultDownloadDir"]).name, "Zulu")
        self.assertEqual(Path(devices[self.other]["defaultDownloadDir"]).name, "Alpha")
        self.engine.save()
        restored = Engine(self.engine.data_dir, start=False, download_dir=self.engine.download_root)
        try:
            self.assertEqual(restored._devices[self.phone]["defaultDownloadDir"], devices[self.phone]["defaultDownloadDir"])
            self.assertEqual(restored._devices[self.other]["downloadNumber"], 2)
        finally:
            restored.close()

    def test_selected_online_or_first_alphabetical_destination(self):
        state = self.engine.state()
        self.assertEqual(Path(state["downloadDir"]).name, "Zulu")
        self.engine._devices[self.phone]["online"] = False
        self.assertEqual(Path(self.engine.state()["downloadDir"]).name, "Alpha")
        self.engine._devices[self.other]["alias"] = "ZZZ"
        self.assertEqual(Path(self.engine.state()["downloadDir"]).name, "Zulu")

    def test_connected_phone_precedes_alphabetical_offline_phone_when_selection_offline(self):
        self.engine._devices[self.phone].update(name="Alpha", online=False)
        self.engine._devices[self.other].update(name="Zulu", online=True)
        self.assertEqual(Path(self.engine.state()["downloadDir"]).name, "Zulu")
        reset = self.engine.handle("reset_download_dir", {})
        self.assertEqual(reset["deviceId"], self.other)

    def test_refresh_identity_keeps_directory_override_and_provider(self):
        from types import SimpleNamespace
        self.engine.state()
        self.engine._devices[self.phone].update(alias="Camera", filesAppId=self.app,
                                                downloadDir=str(self.root / "custom-editing"))
        self.engine._devices[self.other]["online"] = False
        phone_serial = "fixture-one"

        @contextlib.contextmanager
        def lockdown(device, autopair=False):
            self.assertEqual(device, self.phone)
            yield SimpleNamespace(paired=True, all_values={"DeviceName": "New Name"}, get_value=lambda **kw: {})

        with mock.patch("socket.create_connection", return_value=contextlib.nullcontext()), \
                mock.patch("pymobiledevice3.usbmux.list_devices", return_value=[SimpleNamespace(serial=phone_serial, connection_type="USB")]), \
                mock.patch.object(self.engine, "_lockdown", side_effect=lockdown):
            self.engine.poll_devices()
        device = self.engine._devices[self.phone]
        self.assertEqual(device["name"], "New Name")
        self.assertEqual(device["alias"], "Camera")
        self.assertEqual(device["downloadNumber"], 1)
        self.assertEqual(device["filesAppId"], self.app)
        self.assertEqual(device["downloadDir"], str(self.root / "custom-editing"))

    def test_download_uses_its_phone_default_not_other_selected_phone(self):
        self.engine.selected_device = self.other
        self.engine._devices[self.other]["online"] = True
        job = self.download()
        self.assertEqual(Path(job["destination"]).name, "Zulu")

    def test_directory_override_is_per_device_and_reset_restores_default(self):
        custom = self.root / "editing"
        result = self.engine.handle("set_download_dir", {"deviceId": self.phone, "path": str(custom)})
        self.assertEqual(result["deviceId"], self.phone)
        self.assertEqual(self.engine._devices[self.phone]["downloadDir"], str(custom))
        self.assertEqual(Path(self.engine._devices[self.other]["downloadDir"]).name, "Alpha")
        reset = self.engine.handle("reset_download_dir", {"deviceId": self.phone})
        self.assertEqual(reset["path"], reset["defaultPath"])
        self.assertEqual(Path(reset["path"]).name, "Zulu")

    def test_unrelated_global_destination_is_not_used(self):
        self.engine.save()
        saved = json.loads(self.engine.state_file.read_text())
        saved["downloadDir"] = str(self.root / "Videos" / "Unrelated Downloads")
        for device in saved["devices"]:
            for key in ("downloadDir", "defaultDownloadDir", "downloadNumber"):
                device.pop(key, None)
        self.engine.state_file.write_text(json.dumps(saved))
        restored = Engine(self.engine.data_dir, start=False, download_dir=self.engine.download_root)
        try:
            self.assertNotIn("Unrelated Downloads", restored.state()["downloadDir"])
        finally:
            restored.close()

    def test_duplicate_or_invalid_directory_numbers_are_repaired(self):
        self.engine._devices[self.phone]["downloadNumber"] = 2
        self.engine._devices[self.other]["downloadNumber"] = 2
        self.engine.state()
        self.assertNotEqual(self.engine._devices[self.phone]["defaultDownloadDir"], self.engine._devices[self.other]["defaultDownloadDir"])

    def test_startup_invalid_saved_scopes_migrate_without_losing_devices_jobs_or_defaults(self):
        job = self.download("scope-fixture.txt")
        custom = self.root / "custom-editing"
        self.engine.handle("set_download_dir", {"deviceId": self.phone, "path": str(custom)})
        baseline = json.loads(self.engine.state_file.read_text())
        bad_scopes = ["photos", "{malformed", "null", "{}", "123", '"photos"', "[]",
                      json.dumps([self.phone, "photos", ""]),
                      json.dumps([self.phone, "photos", "", "/DCIM", "extra"]),
                      json.dumps([self.phone, "files", "", "/Documents"]),
                      json.dumps([self.phone, "photos", self.app, "/DCIM"]),
                      json.dumps([self.phone, "app", "", "/Documents"]),
                      json.dumps([self.phone, "app", self.app, "/Documents/../private"]),
                      json.dumps([self.phone, "photos", "", "/Documents"]),
                      json.dumps([self.phone, "app", self.app, {"path": "/Documents"}]),
                      json.dumps(["unknown-phone", "photos", "", "/DCIM"]),
                      None, 4, [self.phone, "photos", "", "/DCIM"]]
        for saved_scope in bad_scopes:
            with self.subTest(scope=saved_scope):
                saved = copy.deepcopy(baseline)
                saved["currentScope"] = saved_scope
                self.engine.state_file.write_text(json.dumps(saved))
                restored = Engine(self.engine.data_dir, start=False, download_dir=self.engine.download_root)
                try:
                    state = restored.state()
                    self.assertEqual(state["selectedDevice"], self.phone)
                    self.assertEqual(len(state["devices"]), 2)
                    self.assertEqual(state["currentSource"], "photos")
                    self.assertEqual(state["currentPath"], "/DCIM")
                    self.assertEqual(restored.current_scope, restored.scope(self.phone, "photos"))
                    self.assertEqual(len(state["jobs"]), 1)
                    self.assertEqual(state["jobs"][0]["id"], job["id"])
                    self.assertEqual(state["jobs"][0]["state"], "completed")
                    self.assertEqual(restored._devices[self.phone]["downloadDir"], str(custom))
                    self.assertEqual(Path(restored._devices[self.phone]["defaultDownloadDir"]).name, "Zulu")
                    self.assertEqual(restored._devices[self.other]["downloadNumber"], 2)
                    self.assertEqual(Path(job["localPath"]).read_bytes(), b"original")
                    migrated = json.loads(restored.state_file.read_text())
                    self.assertEqual(migrated["currentScope"], restored.current_scope)
                finally:
                    restored.close()

    def test_startup_valid_app_scope_and_its_catalogue_are_preserved(self):
        job = self.download("valid-scope.txt")
        # Legacy JSON whitespace remains a valid catalogue key.
        scope = json.dumps([self.phone, "app", self.app, "/Documents"])
        self.engine._catalogues[scope] = copy.deepcopy(self.engine._catalogues[self.engine.current_scope])
        self.engine.current_scope = scope
        self.engine.save()
        restored = Engine(self.engine.data_dir, start=False, download_dir=self.engine.download_root)
        try:
            state = restored.state()
            self.assertEqual(restored.current_scope, scope)
            self.assertEqual(state["currentSource"], "app")
            self.assertEqual(state["currentAppId"], self.app)
            self.assertEqual(state["currentPath"], "/Documents")
            self.assertEqual(state["catalogue"][0]["id"], job["item"]["id"])
            self.assertEqual(state["catalogue"][0]["localPath"], job["localPath"])
        finally:
            restored.close()

    def test_startup_invalid_scope_without_selected_phone_uses_none_and_keeps_devices(self):
        self.engine.save()
        saved = json.loads(self.engine.state_file.read_text())
        saved.update(selectedDevice=None, currentScope="photos")
        self.engine.state_file.write_text(json.dumps(saved))
        restored = Engine(self.engine.data_dir, start=False, download_dir=self.engine.download_root)
        try:
            self.assertIsNone(restored.current_scope)
            state = restored.state()
            self.assertIsNone(state["selectedDevice"])
            self.assertEqual(len(state["devices"]), 2)
            self.assertEqual(state["currentSource"], "photos")
        finally:
            restored.close()

    def test_startup_valid_photos_folder_scope_is_preserved(self):
        self.afc.add("/DCIM/100APPLE/cached.jpg", b"cached photo")
        item = self.engine._catalogue_item(self.phone, "photos", None, "/DCIM/100APPLE/cached.jpg", self.afc.stat("/DCIM/100APPLE/cached.jpg"))
        scope = self.engine.scope(self.phone, "photos", path="/DCIM/100APPLE")
        self.engine.current_scope = scope
        self.engine._catalogues[scope] = [item]
        self.engine.save()
        restored = Engine(self.engine.data_dir, start=False, download_dir=self.engine.download_root)
        try:
            state = restored.state()
            self.assertEqual(restored.current_scope, scope)
            self.assertEqual(state["currentPath"], "/DCIM/100APPLE")
            self.assertEqual(state["catalogue"][0]["id"], item["id"])
        finally:
            restored.close()

    def test_files_home_requires_actual_provider_and_does_not_guess_root(self):
        home = self.engine.handle("files_home", {"deviceId": self.phone})
        self.assertTrue(home["requiresChoice"])
        self.assertIsNone(home["path"])
        self.assertEqual(home["apps"][0]["id"], self.app)
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("No provider yet")):
            self.assertTrue(self.engine.ensure_files_home(self.phone)["requiresChoice"])

    def test_configured_files_home_creates_only_provider_eyecopy_folder(self):
        home = self.engine.configure_files_home(self.phone, self.app)
        self.assertEqual(home["path"], "/Documents/EyeCopy")
        self.assertEqual(home["label"], "Documents / EyeCopy")
        self.engine.ensure_files_home(self.phone)
        self.engine.ensure_files_home(self.phone)
        self.assertEqual(self.afc.created, ["/Documents/EyeCopy"])
        self.assertIn("Safari Downloads are unavailable", home["message"])
        self.assertEqual(self.engine._devices[self.phone]["filesAppId"], self.app)

    def test_home_and_upload_preserve_unrelated_device_folders(self):
        # Choosing an upload folder never moves or removes other documents.
        old = "/Documents/Existing Documents"
        self.afc.directories.add(old)
        self.afc.add(old + "/preserved.mov", b"previous phone media")
        self.engine.configure_files_home(self.phone, self.app)
        home = self.engine.ensure_files_home(self.phone)
        self.assertEqual(home["path"], "/Documents/EyeCopy")
        source = self.root / "new-upload.txt"
        source.write_bytes(b"new synthetic upload")
        self.engine.enqueue_upload(self.phone, self.app, home["path"], [str(source)])
        self.engine.run_job(self.engine._jobs[-1])
        self.assertEqual(bytes(self.afc.files[old + "/preserved.mov"]), b"previous phone media")
        self.assertEqual(bytes(self.afc.files["/Documents/EyeCopy/new-upload.txt"]), b"new synthetic upload")
        self.assertEqual(self.afc.removed, [])

    def test_canonical_mobile_companion_exposes_eyecopy_documents_root(self):
        self.engine._apps[self.phone].append({"id": "com.eyecopy.mobile", "name": "EyeCopy"})
        home = self.engine.ensure_files_home(self.phone)
        self.assertTrue(home["nativeEyeCopy"])
        self.assertEqual(home["path"], "/Documents")
        self.assertEqual(home["label"], "EyeCopy")

    def test_native_companion_preferred_over_remembered_provider(self):
        self.engine.configure_files_home(self.phone, self.app)
        self.engine._apps[self.phone].append({"id": "com.eyecopy.iphone", "name": "EyeCopy"})
        home = self.engine.ensure_files_home(self.phone)
        self.assertTrue(home["nativeEyeCopy"])
        self.assertEqual(home["path"], "/Documents")
        self.assertEqual(home["label"], "EyeCopy")
        self.assertEqual(self.afc.created, [])

    def test_unknown_or_removed_provider_is_not_authorized(self):
        with self.assertRaises(UserError):
            self.engine.configure_files_home(self.phone, "private.app")
        self.engine.configure_files_home(self.phone, self.app)
        self.engine._apps[self.phone] = []
        self.assertTrue(self.engine.files_home(self.phone)["requiresChoice"])

    def test_home_folder_collision_or_symlink_is_kept(self):
        self.engine.configure_files_home(self.phone, self.app)
        self.afc.add("/Documents/EyeCopy", b"keep")
        with self.assertRaises(UserError):
            self.engine.ensure_files_home(self.phone)
        self.assertEqual(bytes(self.afc.files["/Documents/EyeCopy"]), b"keep")
        self.afc.files.pop("/Documents/EyeCopy")
        self.afc.links.add("/Documents/EyeCopy")
        with self.assertRaises(UserError):
            self.engine.ensure_files_home(self.phone)

    def test_create_folder_nested_and_no_overwrite(self):
        self.engine.configure_files_home(self.phone, self.app)
        self.engine.ensure_files_home(self.phone)
        result = self.engine.create_folder(self.phone, self.app, "/Documents/EyeCopy", "Edits")
        self.assertEqual(result["path"], "/Documents/EyeCopy/Edits")
        with self.assertRaises(UserError):
            self.engine.create_folder(self.phone, self.app, "/Documents/EyeCopy", "Edits")

    def test_create_folder_invalid_paths_names_and_links(self):
        for name in ("", ".", "..", "../escape", "bad\\name", "bad:folder", "CON", "x.", "x ", "\0bad"):
            with self.subTest(name=name), self.assertRaises(UserError):
                self.engine.create_folder(self.phone, self.app, "/Documents", name)
        for path in ("/", "/Downloads", "/Documents/../private", "/Documents/link"):
            self.afc.links.add("/Documents/link")
            with self.subTest(path=path), self.assertRaises(UserError):
                self.engine.create_folder(self.phone, self.app, path, "Safe")
        self.assertEqual(self.afc.created, [])

    def test_phone_preview_has_no_mutation_and_authoritative_metadata(self):
        item = self.item()
        supplied = {**item, "path": "/Documents/wrong", "size": 999}
        plan = self.engine.preview_phone_delete([supplied])
        self.assertEqual(plan["files"][0]["path"], item["path"])
        self.assertEqual(plan["bytes"], 8)
        self.assertTrue(plan["permanent"])
        self.assertIn("Recently Deleted", plan["warning"])
        self.assertEqual(self.afc.removed, [])

    def test_phone_confirm_multi_file_preserves_pc_and_ownership_index(self):
        jobs = [self.download("one.txt"), self.download("two.txt")]
        paths = [Path(job["localPath"]) for job in jobs]
        plan = self.engine.preview_phone_delete([job["item"] for job in jobs])
        result = self.engine.handle("confirm_phone_delete", {"token": plan["token"]})
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertTrue(all(path.read_bytes() == b"original" for path in paths))
        self.assertEqual(self.engine.state()["catalogue"], [])
        self.assertTrue(all(job["remoteDeleted"] for job in jobs))
        self.assertEqual(len(self.engine.preview_pc_cleanup(self.phone)["files"]), 2)
        self.assertEqual(self.events[-1][0], "state")

    def test_phone_source_changed_aborts_whole_batch(self):
        items = [self.item("one.txt"), self.item("two.txt")]
        plan = self.engine.preview_phone_delete(items)
        self.afc.add(items[1]["path"], b"different bytes")
        with self.assertRaises(SourceChangedError):
            self.engine.confirm_phone_delete(plan["token"])
        self.assertEqual(self.afc.removed, [])
        self.assertTrue(all(self.afc.exists(item["path"]) for item in items))

    def test_phone_plan_expiry_wrong_token_single_use(self):
        item = self.item()
        plan = self.engine.preview_phone_delete([item])
        self.engine._delete_plans[plan["token"]]["expires"] = 0
        with self.assertRaises(UserError):
            self.engine.confirm_phone_delete(plan["token"])
        with self.assertRaises(UserError):
            self.engine.confirm_phone_delete("tampered")
        plan = self.engine.preview_phone_delete([item])
        self.engine.confirm_phone_delete(plan["token"])
        with self.assertRaises(UserError):
            self.engine.confirm_phone_delete(plan["token"])

    def test_phone_changed_catalogue_path_or_removed_provider_aborts(self):
        item = self.item()
        plan = self.engine.preview_phone_delete([item])
        item["path"] = "/Documents/something-else.mov"
        with self.assertRaises(UserError):
            self.engine.confirm_phone_delete(plan["token"])
        self.assertEqual(self.afc.removed, [])
        item["path"] = "/Documents/clip.mov"
        plan = self.engine.preview_phone_delete([item])
        self.engine._apps[self.phone] = []
        with self.assertRaises(UserError):
            self.engine.confirm_phone_delete(plan["token"])
        self.assertEqual(self.afc.removed, [])

    def test_phone_failure_keeps_failed_listing_and_success_removes_only_its_item(self):
        items = [self.item("one.txt"), self.item("two.txt")]
        plan = self.engine.preview_phone_delete(items)
        self.afc.fail_remove = items[1]["path"]
        result = self.engine.confirm_phone_delete(plan["token"])
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual([item["id"] for item in self.engine.state()["catalogue"]], [items[1]["id"]])

    def test_phone_terminal_cache_lock_preserves_truthful_mixed_results_and_emits_state(self):
        jobs = [self.download("kept-pc-one.txt"), self.download("kept-pc-two.txt")]
        items = [job["item"] for job in jobs]
        plan = self.engine.preview_phone_delete(items)
        self.afc.fail_remove = items[1]["path"]
        self.engine.save()
        self.events.clear()
        with mock.patch.object(self.engine, "_save_locked", side_effect=PermissionError("fixture cache lock")) as save, \
                self.assertLogs("eyecopy", level="WARNING"):
            result = self.engine.confirm_phone_delete(plan["token"])
            self.assertEqual(result["deleted"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertTrue(result["stateSavePending"])
            self.assertIn("results are accurate", result["cacheWarning"])
            self.assertEqual(self.events[-1][0], "state")
            self.assertTrue(self.events[-1][1]["stateSavePending"])
            self.assertEqual([item["id"] for item in self.events[-1][1]["catalogue"]], [items[1]["id"]])
            save.assert_called_once()
            self.engine._save_retry_after = 0
            self.assertTrue(self.engine.state()["stateSavePending"])
            self.assertEqual(save.call_count, 2)
            with self.assertRaises(UserError):
                self.engine.confirm_phone_delete(plan["token"])
        self.assertEqual(self.afc.removed, [items[0]["path"]])
        self.assertTrue(all(Path(job["localPath"]).read_bytes() == b"original" for job in jobs))
        self.engine._save_retry_after = 0
        with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("A cache refresh must not delete again")):
            state = self.engine.state()
        self.assertFalse(state["stateSavePending"])
        self.assertIsNone(state["cacheWarning"])
        self.assertEqual(self.afc.removed, [items[0]["path"]])
        persisted = json.loads(self.engine.state_file.read_text())
        self.assertEqual([item["id"] for item in persisted["catalogues"][self.engine.current_scope]], [items[1]["id"]])

    def test_phone_changed_source_check_remains_strict_before_terminal_save(self):
        item = self.item()
        plan = self.engine.preview_phone_delete([item])
        self.afc.add(item["path"], b"changed source")
        with mock.patch.object(self.engine, "_save_mutation_result_locked", side_effect=AssertionError("No terminal save before validation")):
            with self.assertRaises(SourceChangedError):
                self.engine.confirm_phone_delete(plan["token"])
        self.assertEqual(self.afc.removed, [])
        self.assertFalse(self.engine.state()["stateSavePending"])

    def test_phone_photos_folders_links_outside_path_are_protected(self):
        folder = self.engine._catalogue_item(self.phone, "app", self.app, "/Documents", self.afc.stat("/Documents"))
        self.engine._catalogues["folder-fixture"] = [folder]
        with self.assertRaises(UserError):
            self.engine.preview_phone_delete([folder])
        for path, source, app in (("/DCIM/100APPLE/photo.jpg", "photos", None), ("/Documents/link", "app", self.app), ("/private/x", "app", self.app)):
            self.afc.add(path, b"keep")
            item = self.engine._catalogue_item(self.phone, source, app, path, self.afc.stat(path))
            self.engine._catalogues[path] = [item]
            if path.endswith("link"):
                self.afc.links.add(path)
            with self.subTest(path=path), self.assertRaises(UserError):
                self.engine.preview_phone_delete([item])
        self.assertEqual(self.afc.removed, [])

    def test_phone_active_or_queued_download_is_protected(self):
        item = self.item()
        self.engine.enqueue_download(self.phone, [item])
        with self.assertRaises(UserError):
            self.engine.preview_phone_delete([item])
        self.assertEqual(self.afc.removed, [])

    def test_pc_cleanup_scope_only_v2_owned_default_originals(self):
        own = self.download()
        custom = self.download("custom.txt", str(self.root / "custom"))
        default = Path(own["destination"])
        (default / "user-added.txt").write_bytes(b"keep")
        other = Path(self.engine._devices[self.other]["defaultDownloadDir"])
        other.mkdir()
        (other / "other.txt").write_bytes(b"keep")
        plan = self.engine.preview_pc_cleanup(self.phone)
        self.assertEqual(plan["fileCount"], 1)
        self.assertEqual(plan["files"][0]["path"], own["localPath"])
        self.assertNotEqual(plan["path"], custom["destination"])
        self.assertEqual(self.afc.removed, [])

    def test_pc_cleanup_ownership_survives_clear_history_restart_and_phone_delete(self):
        job = self.download()
        self.engine.clear_finished_jobs()
        self.engine.save()
        restored = Engine(self.engine.data_dir, start=False, download_dir=self.engine.download_root)
        try:
            self.assertEqual(restored._jobs, [])
            self.assertEqual(restored.preview_pc_cleanup(self.phone)["fileCount"], 1)
            self.assertEqual(restored._download_records[job["localPath"]]["deviceId"], self.phone)
        finally:
            restored.close()

    def test_pc_cleanup_confirmation_removes_only_plan_fixtures_and_local_associations(self):
        job = self.download()
        remote = job["item"]["path"]
        plan = self.engine.preview_pc_cleanup(self.phone)
        with mock.patch.object(self.engine, "_remove_pc_copy", side_effect=lambda path, validate: path.unlink() if validate() else None) as remove:
            result = self.engine.confirm_pc_cleanup(plan["token"])
        self.assertEqual(result["deleted"], 1)
        remove.assert_called_once()
        self.assertTrue(self.afc.exists(remote))
        self.assertNotIn("localPath", self.engine.state()["catalogue"][0])
        self.assertNotIn("localPath", job)
        self.assertEqual(self.engine._download_records, {})

    def test_pc_terminal_cache_lock_returns_physical_results_and_refresh_retries_save_only(self):
        jobs = [self.download("synthetic-one.txt"), self.download("synthetic-two.txt")]
        untouched = Path(jobs[0]["destination"]) / "user-added.txt"
        untouched.write_bytes(b"keep untracked copy")
        plan = self.engine.preview_pc_cleanup(self.phone)
        self.engine.save()
        self.events.clear()
        with mock.patch.object(self.engine, "_remove_pc_copy", side_effect=lambda path, validate: path.unlink() if validate() else None) as remove:
            with mock.patch.object(self.engine, "_save_locked", side_effect=PermissionError("fixture cache lock")) as save, \
                    self.assertLogs("eyecopy", level="WARNING"):
                result = self.engine.confirm_pc_cleanup(plan["token"])
                self.assertEqual(result["deleted"], 2)
                self.assertEqual(result["failed"], 0)
                self.assertTrue(result["stateSavePending"])
                self.assertIn("Refresh to retry saving", result["cacheWarning"])
                self.assertEqual(remove.call_count, 2)
                save.assert_called_once()
                self.assertEqual(self.events[-1][0], "state")
                self.assertTrue(self.events[-1][1]["stateSavePending"])
                self.assertTrue(all("localPath" not in item for item in self.events[-1][1]["catalogue"]))
                self.assertEqual(self.engine._download_records, {})
                self.assertEqual(len(json.loads(self.engine.state_file.read_text())["downloadRecords"]), 2)
                self.engine._save_retry_after = 0
                self.assertTrue(self.engine.state()["stateSavePending"])
                self.assertEqual(remove.call_count, 2)
                with self.assertRaises(UserError):
                    self.engine.confirm_pc_cleanup(plan["token"])
            self.engine._save_retry_after = 0
            with mock.patch.object(self.engine, "_afc", side_effect=AssertionError("No phone operation during save retry")):
                state = self.engine.state()
            self.assertFalse(state["stateSavePending"])
            self.assertIsNone(state["cacheWarning"])
            self.assertEqual(self.engine.preview_pc_cleanup(self.phone)["fileCount"], 0)
            self.assertEqual(remove.call_count, 2)
        self.assertEqual(json.loads(self.engine.state_file.read_text())["downloadRecords"], {})
        self.assertEqual(untouched.read_bytes(), b"keep untracked copy")
        self.assertTrue(all(self.afc.exists(job["item"]["path"]) for job in jobs))

    def test_pc_changed_original_check_stays_strict_and_does_not_delete_on_save_lock(self):
        job = self.download()
        plan = self.engine.preview_pc_cleanup(self.phone)
        path = Path(job["localPath"])
        path.write_bytes(b"user modified this copy")
        with mock.patch.object(self.engine, "_remove_pc_copy", side_effect=AssertionError("Modified copies must be kept")), \
                mock.patch.object(self.engine, "_save_locked", side_effect=PermissionError("fixture cache lock")), \
                self.assertLogs("eyecopy", level="WARNING"):
            result = self.engine.confirm_pc_cleanup(plan["token"])
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["stateSavePending"])
        self.assertEqual(path.read_bytes(), b"user modified this copy")

    def test_pc_edited_replaced_missing_files_are_kept(self):
        job = self.download()
        plan = self.engine.preview_pc_cleanup(self.phone)
        Path(job["localPath"]).write_bytes(b"edited file")
        with mock.patch.object(self.engine, "_remove_pc_copy", side_effect=AssertionError("Keep edited file")):
            result = self.engine.confirm_pc_cleanup(plan["token"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(Path(job["localPath"]).read_bytes(), b"edited file")
        self.assertEqual(self.engine.preview_pc_cleanup(self.phone)["fileCount"], 0)
        Path(job["localPath"]).unlink()
        self.assertEqual(self.engine.preview_pc_cleanup(self.phone)["fileCount"], 0)

    def test_pc_inherited_completed_job_does_not_authorize_cleanup(self):
        item = self.item()
        destination = self.root / "Videos" / "Unrelated Downloads"
        destination.mkdir(parents=True)
        path = destination / "original.mov"
        path.write_bytes(b"untracked original")
        self.engine._jobs.append(self.engine._new_job(path.name, "download", self.phone, 11,
                                item=item, state="completed", localPath=str(path), destination=str(destination)))
        self.assertEqual(self.engine.preview_pc_cleanup(self.phone)["fileCount"], 0)

    def test_pc_recycle_failure_does_not_remove_or_forget_owned_copy(self):
        job = self.download()
        plan = self.engine.preview_pc_cleanup(self.phone)
        with mock.patch.object(self.engine, "_remove_pc_copy", side_effect=UserError("Recycle unavailable")):
            result = self.engine.confirm_pc_cleanup(plan["token"])
        self.assertEqual(result["failed"], 1)
        self.assertTrue(Path(job["localPath"]).is_file())
        self.assertIn(job["localPath"], self.engine._download_records)

    def test_pc_nonordinary_and_reparse_paths_are_rejected(self):
        job = self.download()
        root = Path(job["destination"])
        self.assertFalse(self.engine._ordinary_pc_file(root / ".." / "escape", root))
        self.assertFalse(self.engine._ordinary_pc_file(root, root))
        if os.name != "nt":
            alias = root / "alias.mov"
            alias.symlink_to(Path(job["localPath"]))
            self.assertFalse(self.engine._ordinary_pc_file(alias, root))

    def test_pc_cleanup_expiry_wrong_kind_and_record_changed_are_kept(self):
        job = self.download()
        plan = self.engine.preview_pc_cleanup(self.phone)
        self.engine._delete_plans[plan["token"]]["expires"] = 0
        with self.assertRaises(UserError):
            self.engine.confirm_pc_cleanup(plan["token"])
        plan = self.engine.preview_pc_cleanup(self.phone)
        self.engine._download_records[job["localPath"]]["itemId"] = "new-record"
        with mock.patch.object(self.engine, "_remove_pc_copy", side_effect=AssertionError("Changed record kept")):
            result = self.engine.confirm_pc_cleanup(plan["token"])
        self.assertEqual(result["failed"], 1)
        self.assertTrue(Path(job["localPath"]).is_file())

    def test_pc_reparse_attribute_protects_path_and_parent(self):
        job = self.download()
        path, root = Path(job["localPath"]), Path(job["destination"])
        original = Path.lstat
        from types import SimpleNamespace
        for marked in (path, root, root.parent):
            def lstat(candidate):
                return SimpleNamespace(st_file_attributes=0x400, st_mode=original(candidate).st_mode) if candidate == marked else original(candidate)
            with self.subTest(marked=str(marked)), mock.patch.object(Path, "lstat", side_effect=lstat, autospec=True):
                self.assertFalse(self.engine._ordinary_pc_file(path, root))


if __name__ == "__main__":
    unittest.main()
