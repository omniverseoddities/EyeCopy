"""Exercise uninstall safeguards with disposable files and an isolated registry key."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import unittest
import uuid
import winreg

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "build-output" / "installer" / "ProfileCleanup.exe"
APP_ID = "com.eyecopy.desktop"
TEST_REGISTRY = r"Software\EyeCopy\InstallerTests"


def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(path):
    folded=str(path).translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))
    return hashlib.sha256(folded.encode("utf-8")).hexdigest()


class CleanupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "test-output" / "uninstall-safety" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.marker(self.root, ".eyecopy-uninstall-test.json", "test")
        self.app = self.root / "application"
        self.app.mkdir()
        self.marker(self.app, ".eyecopy-install.json", "application")
        (self.app / "EyeCopy.exe").write_bytes(b"synthetic application; not executable")
        (self.app / "UninstallEyeCopy.exe").write_bytes(b"synthetic uninstaller; not executable")
        (self.app / "README.txt").write_text("Synthetic package fixture", encoding="utf-8")
        self.manifest()
        self.registry = TEST_REGISTRY + "\\" + identity(self.root)
        self.profiles = self.registry + r"\Profiles"
        self.keys = []

    def tearDown(self):
        # Only this test's newly created registration keys are removed.
        self.assertTrue(self.registry.startswith(TEST_REGISTRY + "\\"))
        for key in self.keys:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.profiles + "\\" + key)
            except FileNotFoundError:
                pass
        for key in (self.profiles, self.registry):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except FileNotFoundError:
                pass

    def marker(self, directory, name, kind, app_id=APP_ID):
        (directory / name).write_text(json.dumps({"applicationId": app_id, "schema": 1, "kind": kind}), encoding="utf-8")

    def manifest(self):
        files = {str(p.relative_to(self.app)): checksum(p) for p in self.app.rglob("*")
                 if p.is_file() and p.name != ".eyecopy-build.json"}
        (self.app / ".eyecopy-build.json").write_text(json.dumps({"application": "EyeCopy", "sourceFiles": files}), encoding="utf-8")

    def profile(self, name="profile", state=None):
        profile = self.root / name
        profile.mkdir()
        self.marker(profile, ".eyecopy-profile.json", "profile")
        (profile / "state.json").write_text(json.dumps(state or {"devices": [], "jobs": [], "downloadRecords": {}}), encoding="utf-8")
        return profile

    def register(self, profile, key=None):
        name = key or identity(profile)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.profiles + "\\" + name) as handle:
            winreg.SetValueEx(handle, "Path", 0, winreg.REG_SZ, str(profile))
        self.keys.append(name)

    def run_helper(self, *extra, execute=True):
        args = [str(HELPER), "--app-root", str(self.app), "--test-root", str(self.root)]
        args.extend(["--cleanup", "--confirmed"] if execute else ["--preview"])
        run = subprocess.run([*args, *extra], capture_output=True, text=True, encoding="utf-8",
                             creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        result = json.loads(run.stdout)
        (self.root / ("last-cleanup.json" if execute else "last-preview.json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
        return run.returncode, result

    def junction(self, target, link):
        node = ROOT / ".tools" / "node-v24.11.1-win-x64" / "node.exe"
        if not node.is_file():
            node = Path("node")
        subprocess.run([str(node), "-e", "require('fs').symlinkSync(process.argv[1],process.argv[2],'junction')",
                        str(target), str(link)], check=True, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)

    def test_normal_profile_cleanup_keeps_outside_download(self):
        download = self.root / "downloads" / "original.mov"
        download.parent.mkdir()
        download.write_bytes(b"synthetic original")
        profile = self.profile(state={"downloadRecords": {str(download): {"path": str(download)}}})
        (profile / "cache").mkdir()
        (profile / "cache" / "preview.jpg").write_bytes(b"synthetic preview")
        (profile / "desktop.log").write_text("synthetic log", encoding="utf-8")
        self.register(profile)
        rc, result = self.run_helper()
        self.assertEqual(rc, 0, result)
        self.assertFalse(profile.exists())
        self.assertEqual(download.read_bytes(), b"synthetic original")
        self.assertFalse((self.app / "EyeCopy.exe").exists())

    def test_unicode_profile_registration_uses_ascii_only_path_fold(self):
        profile=self.profile(name="\u0130-studio-PROFILE")
        self.register(profile)
        (profile/'cache').mkdir()
        (profile/'cache'/'preview.jpg').write_bytes(b'Synthetic preview')
        self.assertNotEqual(identity(profile),hashlib.sha256(str(profile).lower().encode('utf-8')).hexdigest())
        node=ROOT/'.tools/node-v24.11.1-win-x64/node.exe'
        if not node.is_file():node=Path('node')
        code="const {registerProfile}=require('./desktop/profile-ownership');const value=registerProfile(process.argv[1],{applicationRoot:process.argv[2],execute(){return '';}});console.log(JSON.stringify(value));"
        run=subprocess.run([str(node),'-e',code,str(profile),str(self.app)],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW,check=True)
        registration=json.loads(run.stdout)
        self.assertTrue(registration['registered'],registration)
        self.assertEqual(registration['registryKey'].split('\\')[-1],identity(profile))
        status,result=self.run_helper()
        self.assertEqual(status,0,result)
        self.assertFalse(profile.exists())

    def test_download_and_unknown_files_inside_application_are_preserved(self):
        download = self.app / "user-downloads" / "original.mov"
        download.parent.mkdir()
        download.write_bytes(b"unmanifested original")
        unknown = self.app / "personal.txt"
        unknown.write_bytes(b"unmanifested document")
        rc, result = self.run_helper()
        self.assertEqual(rc, 0, result)
        self.assertEqual(download.read_bytes(), b"unmanifested original")
        self.assertEqual(unknown.read_bytes(), b"unmanifested document")
        self.assertFalse((self.app / "README.txt").exists())

    def test_tracked_download_inside_profile_cache_survives(self):
        profile = self.profile()
        download = profile / "cache" / "original.mov"
        download.parent.mkdir()
        download.write_bytes(b"tracked original")
        state = {"downloadRecords": {str(download): {"path": str(download)}}}
        (profile / "state.json").write_text(json.dumps(state), encoding="utf-8")
        self.register(profile)
        rc, result = self.run_helper()
        self.assertEqual(rc, 0, result)
        self.assertEqual(download.read_bytes(), b"tracked original")
        self.assertTrue(result["kept"])

    def test_unknown_profile_item_is_preserved(self):
        profile = self.profile()
        (profile / "unknown.txt").write_bytes(b"keep")
        self.register(profile)
        rc, result = self.run_helper()
        self.assertEqual(rc, 0, result)
        self.assertEqual((profile / "unknown.txt").read_bytes(), b"keep")
        self.assertFalse((profile / "state.json").exists())
        self.assertTrue(result["kept"])

    def test_profile_junction_child_aborts_before_any_deletion(self):
        profile = self.profile()
        (profile / "cache").mkdir()
        sentinel = self.root / "outside"
        sentinel.mkdir()
        (sentinel / "sentinel.txt").write_bytes(b"keep")
        self.junction(sentinel, profile / "cache" / "redirected")
        self.register(profile)
        rc, result = self.run_helper()
        self.assertNotEqual(rc, 0, result)
        self.assertEqual((sentinel / "sentinel.txt").read_bytes(), b"keep")
        self.assertTrue((profile / "state.json").exists())
        self.assertTrue((self.app / "EyeCopy.exe").exists())

    def test_profile_junction_ancestor_aborts_before_any_deletion(self):
        actual = self.root / "actual"
        actual.mkdir()
        profile = actual / "profile"
        profile.mkdir()
        self.marker(profile, ".eyecopy-profile.json", "profile")
        (profile / "state.json").write_text("{}", encoding="utf-8")
        link = self.root / "redirected"
        self.junction(actual, link)
        self.register(link / "profile")
        rc, result = self.run_helper()
        self.assertNotEqual(rc, 0, result)
        self.assertTrue((profile / "state.json").exists())
        self.assertTrue((self.app / "EyeCopy.exe").exists())

    def test_unknown_application_junction_is_not_followed(self):
        sentinel = self.root / "outside"
        sentinel.mkdir()
        (sentinel / "sentinel.txt").write_bytes(b"keep")
        self.junction(sentinel, self.app / "unmanifested-link")
        rc, result = self.run_helper()
        self.assertEqual(rc, 0, result)
        self.assertEqual((sentinel / "sentinel.txt").read_bytes(), b"keep")
        self.assertTrue((self.app / "unmanifested-link").exists())
        self.assertTrue(result["kept"])

    def test_foreign_profile_marker_is_rejected(self):
        profile = self.profile()
        self.marker(profile, ".eyecopy-profile.json", "profile", "com.example.other")
        self.register(profile)
        rc, result = self.run_helper()
        self.assertNotEqual(rc, 0, result)
        self.assertTrue((profile / "state.json").exists())
        self.assertTrue((self.app / "EyeCopy.exe").exists())

    def test_bad_registration_identity_is_rejected(self):
        profile = self.profile()
        self.register(profile, "0" * 64)
        rc, result = self.run_helper()
        self.assertNotEqual(rc, 0, result)
        self.assertTrue((profile / "state.json").exists())

    def test_drive_relative_profile_registration_is_rejected(self):
        self.register(Path("C:relative"))
        rc, result = self.run_helper()
        self.assertNotEqual(rc, 0, result)
        self.assertTrue((self.app / "EyeCopy.exe").exists())

    def test_posix_device_paths_do_not_protect_entire_local_drive(self):
        profile = self.profile(state={"catalogue": [{"path": "/"}, {"path": "/Documents/file.mov"}]})
        (profile / "cache").mkdir()
        (profile / "cache" / "preview.jpg").write_bytes(b"preview")
        self.register(profile)
        rc, result = self.run_helper()
        self.assertEqual(rc, 0, result)
        self.assertFalse(profile.exists())

    def test_locked_cache_retains_history_and_can_be_retried(self):
        profile = self.profile()
        (profile / "cache").mkdir()
        cached = profile / "cache" / "preview.jpg"
        cached.write_bytes(b"preview")
        self.register(profile)
        with cached.open("rb"):
            rc, result = self.run_helper()
            self.assertNotEqual(rc, 0, result)
            self.assertTrue((profile / "state.json").exists())
            self.assertTrue((self.app / "EyeCopy.exe").exists())
        rc, result = self.run_helper()
        self.assertEqual(rc, 0, result)
        self.assertFalse(profile.exists())

    def test_report_cannot_escape_synthetic_root(self):
        target = self.root.parent / (self.root.name + "-outside.json")
        rc, result = self.run_helper("--report", str(target))
        self.assertNotEqual(rc, 0, result)
        self.assertFalse(target.exists())
        self.assertTrue((self.app / "EyeCopy.exe").exists())

    def test_missing_history_with_cache_is_retained_for_review(self):
        profile = self.profile()
        (profile / "state.json").unlink()
        (profile / "cache").mkdir()
        (profile / "cache" / "unknown.mov").write_bytes(b"keep")
        self.register(profile)
        rc, result = self.run_helper()
        self.assertNotEqual(rc, 0, result)
        self.assertEqual((profile / "cache" / "unknown.mov").read_bytes(), b"keep")


if __name__ == "__main__":
    if sys.platform != "win32" or not HELPER.is_file():
        raise SystemExit("Build the Windows installer cleanup helper before running this suite.")
    unittest.main(verbosity=2)
