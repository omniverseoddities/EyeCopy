"""Exact owned-output notifications; never open an iPhone or operate windows."""
import tempfile
import unittest
from pathlib import Path, PureWindowsPath

from backend.shell_notify import notify_local_download, CREATE, UPDATEDIR, FLUSHNOWAIT, PATHW


class FakeShell:
    def __init__(self, initialized=0, parse_supported=True, fail_event=None):
        self.initialized, self.parse_supported, self.fail_event = initialized, parse_supported, fail_event
        self.calls = []

    def initialize(self):
        self.calls.append(("initialize",))
        return self.initialized

    def uninitialize(self):
        self.calls.append(("uninitialize",))

    def parse(self, path):
        self.calls.append(("parse", path))
        return "pidl:" + path if self.parse_supported else None

    def free(self, pidl):
        self.calls.append(("free", pidl))

    def notify_id(self, event, pidl):
        self.calls.append(("id", event, FLUSHNOWAIT, pidl))
        if event == self.fail_event:
            raise OSError("Notification unavailable")

    def notify_path(self, event, path):
        self.calls.append(("path", event, FLUSHNOWAIT | PATHW, path))


class ShellNotifyTests(unittest.TestCase):
    def test_unicode_pidls_notify_only_exact_output_and_parent_then_release_and_balance(self):
        path = "C:\\日本語🎞\\original.mov"
        api = FakeShell(initialized=1)
        self.assertTrue(notify_local_download(path, bindings=api))
        parent = str(PureWindowsPath(path).parent)
        self.assertEqual(api.calls, [("initialize",), ("parse", path), ("id", CREATE, 0x3000, "pidl:" + path),
                                     ("free", "pidl:" + path), ("parse", parent), ("id", UPDATEDIR, 0x3000, "pidl:" + parent),
                                     ("free", "pidl:" + parent), ("uninitialize",)])

    def test_existing_other_apartment_is_used_without_uninitializing_it(self):
        api = FakeShell(initialized=-2147417850)
        self.assertTrue(notify_local_download("C:\\output\\video.mov", bindings=api))
        self.assertTrue(any(call[0] == "parse" for call in api.calls))
        self.assertFalse(any(call[0] == "uninitialize" for call in api.calls))

    def test_short_unicode_path_fallback_has_flushnowait_and_no_ansi_conversion(self):
        api = FakeShell(parse_supported=False)
        path = "C:\\é\\動画.mov"
        self.assertTrue(notify_local_download(path, bindings=api))
        self.assertEqual([call for call in api.calls if call[0] == "path"],
                         [("path", CREATE, 0x3005, path), ("path", UPDATEDIR, 0x3005, "C:\\é")])

    def test_long_paths_use_pidls_and_never_truncate_or_fall_back_to_path_strings(self):
        path = "C:\\" + "🎞" * 140 + "\\video.mov"
        api = FakeShell()
        self.assertTrue(notify_local_download(path, bindings=api))
        self.assertEqual(len([call for call in api.calls if call[0] == "id"]), 2)
        self.assertFalse(any(call[0] == "path" for call in api.calls))
        unsupported = FakeShell(parse_supported=False)
        self.assertFalse(notify_local_download(path, bindings=unsupported))
        self.assertFalse(any(call[0] in {"id", "path"} for call in unsupported.calls))

    def test_failed_create_notice_still_releases_its_pidl_and_notifies_parent(self):
        api = FakeShell(fail_event=CREATE)
        self.assertTrue(notify_local_download("C:\\output\\video.mov", bindings=api))
        self.assertEqual(len([call for call in api.calls if call[0] == "free"]), 2)
        self.assertTrue(any(call[0] == "id" and call[1] == UPDATEDIR for call in api.calls))
        self.assertEqual(api.calls[-1], ("uninitialize",))

    def test_relative_device_network_and_embedded_null_paths_do_not_touch_shell(self):
        for path in ("relative.mov", "\\\\server\\share\\video.mov", "\\\\?\\GLOBALROOT\\Device\\disk", "C:\\bad\0.mov"):
            api = FakeShell()
            self.assertFalse(notify_local_download(path, bindings=api))
            self.assertEqual(api.calls, [])

    def test_cleanup_failures_cannot_escape_best_effort_helper_or_skip_parent_notice(self):
        api = FakeShell()
        def broken_free(pidl):
            api.calls.append(("free", pidl))
            raise OSError("Cleanup unavailable")
        def broken_uninitialize():
            api.calls.append(("uninitialize",))
            raise OSError("Apartment teardown unavailable")
        api.free, api.uninitialize = broken_free, broken_uninitialize
        self.assertTrue(notify_local_download("C:\\output\\video.mov", bindings=api))
        self.assertEqual(len([call for call in api.calls if call[0] == "id"]), 2)
        self.assertEqual(len([call for call in api.calls if call[0] == "free"]), 2)
        self.assertEqual(api.calls[-1], ("uninitialize",))

    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows Shell integration")
    def test_real_notification_submits_for_private_unicode_file_without_changing_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "動画"
            folder.mkdir()
            output = folder / "original.mov"
            output.write_bytes(b"private shell notification check")
            self.assertTrue(notify_local_download(str(output)))
            self.assertEqual(output.read_bytes(), b"private shell notification check")
            self.assertEqual(list(folder.iterdir()), [output])
