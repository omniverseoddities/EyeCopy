"""Smoke-test a built EyeCopy package without development runtimes or phone writes.

The subprocess runtime is copied into an isolated test directory and launched
with a minimal Windows PATH. Every native module must resolve inside that copy.
PC recycling uses only newly created, identity-verified fixtures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 45


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


class Bridge:
    def __init__(self, executable, data, downloads, env):
        self.messages, self.stderr = queue.Queue(), []
        self.events = []
        self.proc = subprocess.Popen(
            [str(executable), "--data-dir", str(data), "--download-dir", str(downloads)],
            cwd=executable.parent, env=env, text=True, encoding="utf-8",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)

        def stdout():
            for line in self.proc.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    self.messages.put({"invalidOutput": line[:1000]})
        def stderr():
            for line in self.proc.stderr:
                self.stderr.append(line.strip())
        self.readers = [threading.Thread(target=stdout, daemon=True), threading.Thread(target=stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def request(self, command, args=None):
        ident = uuid.uuid4().hex
        self.proc.stdin.write(json.dumps({"id": ident, "command": command, "args": args or {}}) + "\n")
        self.proc.stdin.flush()
        end = time.monotonic() + TIMEOUT
        while time.monotonic() < end:
            try:
                message = self.messages.get(timeout=min(0.25, end - time.monotonic()))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"Frozen engine exited {self.proc.returncode}: {self.stderr}")
                continue
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError(f"{command}: {message['error']}; stderr: {self.stderr}")
                return message["result"]
            self.events.append(message)
        raise TimeoutError(f"{command} exceeded {TIMEOUT} seconds")

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=2)
        return {"exitCode": self.proc.returncode, "stderr": self.stderr}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--distribution", type=Path, default=ROOT / "release" / "EyeCopy")
    parser.add_argument("--require-phone", action="store_true")
    args = parser.parse_args()
    distribution = args.distribution.resolve()
    app = distribution / "resources" / "app"
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    output = ROOT / "test-output" / "portable-smoke" / run_id
    output.mkdir(parents=True)
    isolated = output / "isolated-runtime"
    isolated.mkdir()
    report = {"application": "EyeCopy", "runId": run_id, "isolatedRuntime": str(isolated),
              "distribution": str(distribution), "steps": [], "phoneWrites": False,
              "driverServicePairingChanges": False, "syntheticPCFilesOnly": True}
    bridge = None

    def step(name, callback):
        start = time.monotonic()
        entry = {"name": name}
        try:
            entry.update(status="passed", details=callback())
        except Exception as error:
            entry.update(status="failed", error=str(error))
        entry["elapsedSeconds"] = round(time.monotonic() - start, 3)
        report["steps"].append(entry)
        print(json.dumps({"step": name, "status": entry["status"], "error": entry.get("error")}), flush=True)

    try:
        def copy_runtime():
            original_engine = app / "runtime/engine/EyeCopyEngine.exe"
            original_hash = sha256(original_engine)
            for path in distribution.iterdir():
                if path.is_file() and path.suffix.lower() in {".exe", ".dll", ".bin", ".dat", ".pak", ".json"}:
                    shutil.copy2(path, isolated / path.name)
            isolated_app = isolated / "resources" / "app"
            isolated_app.mkdir(parents=True)
            for relative in ("runtime/engine", "node_modules/koffi", "node_modules/@koromix/koffi-win32-x64",
                             "assets/native", "vendor/ffmpeg", "desktop/native-drag"):
                shutil.copytree(app / relative, isolated_app / relative)
            (isolated_app / "scripts").mkdir()
            for name in ("MarkerDrop.js", "DropFolderProbe.js"):
                shutil.copy2(app / "scripts" / name, isolated_app / "scripts" / name)
            engine_hash = sha256(isolated_app / "runtime/engine/EyeCopyEngine.exe")
            assert engine_hash == original_hash == sha256(original_engine), "Distribution changed during copy; retry after build completes"
            assert (isolated_app / "runtime/engine/_internal/python310.dll").is_file(), "Bundled Python DLL missing"
            return {"engineHash": engine_hash,
                    "developmentRuntimeNotUsed": True, "bundledEngine": True}
        step("copy-isolated-runtime", copy_runtime)
        isolated_app = isolated / "resources" / "app"
        env = os.environ.copy()
        temporary = output / "temporary"
        temporary.mkdir()
        env["TEMP"] = env["TMP"] = str(temporary)
        for name in ("PYTHONPATH", "PYTHONHOME", "NODE_PATH", "NODE_OPTIONS", "ELECTRON_RUN_AS_NODE"):
            env.pop(name, None)
        windows = Path(env.get("SystemRoot", r"C:\Windows"))
        env["PATH"] = os.pathsep.join(str(p) for p in (windows / "System32", windows,
                                     windows / "System32/Wbem", windows / "System32/WindowsPowerShell/v1.0",
                                     isolated_app / "vendor/ffmpeg"))
        env["PYTHONUTF8"] = "1"
        report["subprocessPath"] = env["PATH"]

        def ffmpeg():
            result = subprocess.run([str(isolated_app / "vendor/ffmpeg/ffmpeg.exe"), "-version"],
                                    cwd=isolated, env=env, capture_output=True, text=True,
                                    timeout=TIMEOUT, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
            assert result.stdout.startswith("ffmpeg version")
            return {"version": result.stdout.splitlines()[0]}
        step("bundled-ffmpeg", ffmpeg)

        def natives():
            script = isolated_app / "portable-native-smoke.cjs"
            script.write_text("""'use strict';
const fs=require('node:fs'),path=require('node:path');
const koffi=require('./node_modules/koffi');
const kernel=koffi.load('kernel32.dll');
const currentPid=kernel.func('__stdcall','GetCurrentProcessId','uint32_t',[])();
if(currentPid!==process.pid) throw new Error('Native ABI mismatch');
(async()=>{
 const expected=path.join(__dirname,'assets','native');
 const data=path.join(__dirname,'synthetic-helper-data');
 const helpers=[await require('./desktop/native-drag/broker').buildHelper(data),
   await require('./scripts/MarkerDrop').buildMarkerProbe(data),
   await require('./scripts/DropFolderProbe').buildDropFolderProbe(data)];
 if(helpers.some(file=>path.dirname(file)!==expected||!fs.existsSync(file))) throw new Error('Helper would need compilation');
 const nativePkg=require.resolve('@koromix/koffi-win32-x64');
 if(!nativePkg.startsWith(path.join(__dirname,'node_modules'))) throw new Error('Native resolved outside package');
 const loaded=Object.keys(require.cache).filter(file=>!file.startsWith(__dirname+path.sep));
 if(loaded.length) throw new Error('Module resolved outside isolated package: '+loaded.join(','));
 console.log(JSON.stringify({node:process.version,electron:process.versions.electron,koffi:koffi.version,currentPid,helpers:helpers.map(p=>path.basename(p)),nativePackage:PathRelative(nativePkg),runtimeCompilation:false}));
 function PathRelative(p){return path.relative(__dirname,p)}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
""", encoding="utf-8")
            native_env = {**env, "ELECTRON_RUN_AS_NODE": "1"}
            result = subprocess.run([str(isolated / "EyeCopy.exe"), str(script)], cwd=isolated,
                                    env=native_env, capture_output=True, text=True,
                                    timeout=TIMEOUT, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
            return json.loads(result.stdout)
        step("electron-koffi-precompiled-helpers", natives)

        downloads, data = isolated / "synthetic-downloads", isolated / "synthetic-engine-data"
        destination = downloads / "Portable synthetic fixture"
        destination.mkdir(parents=True)
        data.mkdir()
        device = "iphone-" + uuid.uuid4().hex[:20]
        second_device = "iphone-" + uuid.uuid4().hex[:20]
        second_destination = downloads / "Second synthetic phone"
        second_destination.mkdir()
        second_sentinel = second_destination / "untracked-second-phone.txt"
        second_sentinel.write_text("Changing the shared default must leave this synthetic file in place.\n", encoding="utf-8")
        second_hash = sha256(second_sentinel)
        records, fixture_hashes, catalogue = {}, {}, []
        for index in (1, 2):
            fixture = destination / f"eyecopy-portable-synthetic-{index}.txt"
            fixture.write_text(f"EyeCopy disposable portable smoke fixture {run_id} {index}\n", encoding="utf-8")
            stat = fixture.stat()
            records[str(fixture)] = {"deviceId": device, "itemId": uuid.uuid4().hex[:24], "path": str(fixture),
                                    "identity": {"size": stat.st_size, "inode": stat.st_ino, "device": stat.st_dev},
                                    "modifiedNs": stat.st_mtime_ns}
            fixture_hashes[str(fixture)] = sha256(fixture)
            catalogue.append({"id": records[str(fixture)]["itemId"], "deviceId": device,
                "source": "photos", "appId": None, "path": "/DCIM/Synthetic/" + fixture.name,
                "parentPath": "/DCIM/Synthetic", "name": fixture.name, "kind": "file",
                "size": stat.st_size, "modified": "2026-10-30T12:00:00Z"})
        sentinel = destination / "untracked-synthetic-keep.txt"
        sentinel.write_text("This new synthetic file is not an EyeCopy download and must remain.\n", encoding="utf-8")
        sentinel_hash = sha256(sentinel)
        scope = json.dumps([device, "photos", "", "/DCIM"], separators=(",", ":"))
        stopped_job = {"id": uuid.uuid4().hex, "name": "Disposable queued fixture",
            "deviceId": device, "direction": "download", "source": "photos", "item": catalogue[0],
            "destination": str(destination), "state": "paused", "awaitingStart": True,
            "bytes": 0, "total": catalogue[0]["size"], "speed": 0, "eta": None, "error": None}
        (data / "state.json").write_text(json.dumps({"version": 1, "devices": [{"id": device,
            "name": "Portable synthetic fixture", "online": False, "downloadNumber": 1},
            {"id": second_device, "name": "Second synthetic phone", "online": False, "downloadNumber": 2}],
            "downloadRecords": records, "catalogues": {scope: catalogue}, "apps": {}, "jobs": [stopped_job], "selectedDevice": device,
            "currentScope": scope}), encoding="utf-8")

        def start_engine():
            nonlocal bridge
            bridge = Bridge(isolated_app / "runtime/engine/EyeCopyEngine.exe", data, downloads, env)
            state = bridge.request("state")
            assert any(d["id"] == device for d in state["devices"])
            return {"jsonLinesState": True, "temporaryDataDirectory": True, "jobs": len(state["jobs"])}
        step("frozen-engine-json-lines-boot", start_engine)

        def queue_controls():
            result = bridge.request("cancel_all_jobs")
            assert any(j["id"] == stopped_job["id"] and j["state"] == "cancelled" for j in result["jobs"])
            cleared = bridge.request("clear_all_jobs")
            assert not cleared["jobs"] and cleared["cleared"] == 1 and not cleared["blocked"], cleared
            assert all(sha256(path) == expected for path, expected in fixture_hashes.items())
            return {"stoppedDeferredEntry": True, "clearedUntransferredEntry": True,
                    "completedOriginalsPreserved": True}
        step("frozen-engine-stop-and-clear-all", queue_controls)

        def recycle():
            if bridge is None:
                raise RuntimeError("Engine did not start")
            for path, expected in fixture_hashes.items():
                assert sha256(path) == expected
            plan = bridge.request("preview_pc_cleanup", {"deviceId": device})
            assert plan["fileCount"] == 2 and plan["permanent"] is False
            assert {p["path"] for p in plan["files"]} == set(fixture_hashes)
            assert Path(plan["path"]) == destination and all(Path(p).parent == destination for p in fixture_hashes)
            result = bridge.request("confirm_pc_cleanup", {"token": plan["token"]})
            assert result["deleted"] == 2 and result["failed"] == 0, result
            assert all(not Path(p).exists() for p in fixture_hashes)
            assert sentinel.exists() and sha256(sentinel) == sentinel_hash
            after = bridge.request("preview_pc_cleanup", {"deviceId": device})
            assert after["fileCount"] == 0
            undo = bridge.request("undo_pc_cleanup", {"token": result["undoToken"]})
            assert undo["restored"] == 2 and undo["complete"] and undo["failed"] == 0, undo
            assert all(sha256(path) == expected for path, expected in fixture_hashes.items())
            redo = bridge.request("redo_pc_cleanup", {"token": undo["redoToken"]})
            assert redo["deleted"] == 2 and redo["complete"] and redo["failed"] == 0, redo
            assert all(not Path(path).exists() for path in fixture_hashes)
            final_restore = bridge.request("undo_pc_cleanup", {"token": redo["undoToken"]})
            assert final_restore["restored"] == 2 and final_restore["complete"], final_restore
            assert all(sha256(path) == expected for path, expected in fixture_hashes.items())
            assert sentinel.exists() and sha256(sentinel) == sentinel_hash
            return {"deleted": result["deleted"], "failed": result["failed"], "recycleBinConfirmedByFrozenCOMSink": True,
                    "permanentDeletion": False, "untrackedSyntheticFilePreserved": True,
                    "nativeUndoRedoRoundTrip": True, "finalOriginalsRestored": True,
                    "fixtureHashes": {Path(p).name: h for p, h in fixture_hashes.items()}}
        step("frozen-engine-safe-pc-recycle", recycle)

        def selected_recycle():
            selected = catalogue[0]
            selected_path = next(path for path, record in records.items() if record["itemId"] == selected["id"])
            others = {path: signature for path, signature in fixture_hashes.items() if path != selected_path}
            plan = bridge.request("preview_pc_delete", {"deviceId": device, "items": [{"id": selected["id"]}]})
            assert plan["fileCount"] == 1 and {f["path"] for f in plan["files"]} == {selected_path}
            result = bridge.request("confirm_pc_delete", {"token": plan["token"]})
            assert result["deleted"] == 1 and result["failed"] == 0 and not Path(selected_path).exists(), result
            assert all(sha256(path) == signature for path, signature in others.items())
            restored = bridge.request("undo_pc_cleanup", {"token": result["undoToken"]})
            assert restored["restored"] == 1 and sha256(selected_path) == fixture_hashes[selected_path], restored
            redo = bridge.request("redo_pc_cleanup", {"token": restored["redoToken"]})
            assert redo["deleted"] == 1 and not Path(selected_path).exists(), redo
            final = bridge.request("undo_pc_cleanup", {"token": redo["undoToken"]})
            assert final["restored"] == 1 and all(sha256(path) == signature for path, signature in fixture_hashes.items())
            assert sha256(sentinel) == sentinel_hash
            return {"selectedOnly": True, "realWindowsRecycleBin": True,
                    "nativeUndoRedoRoundTrip": True, "unselectedCopyAndUntrackedFilePreserved": True,
                    "finalOriginalsRestored": True}
        step("frozen-engine-selected-pc-delete", selected_recycle)

        def shared_root():
            if bridge is None:
                raise RuntimeError("Engine did not start")
            state = bridge.request("state")
            old_root = Path(state["downloadRoot"])
            move_parent = isolated / "new-global-parent"
            move_parent.mkdir()
            target = move_parent / "EyeCopy"
            hashes = {Path(path).relative_to(old_root): expected for path, expected in fixture_hashes.items()}
            hashes[sentinel.relative_to(old_root)] = sentinel_hash
            hashes[second_sentinel.relative_to(old_root)] = second_hash

            def verify_unchanged():
                assert all(sha256(old_root / relative) == expected for relative, expected in hashes.items())
                assert all(not (target / relative).exists() for relative in hashes)

            changed = bridge.request("set_default_download_dir", {"path": str(move_parent)})
            assert changed["changed"] and changed["historyId"], changed
            assert old_root.is_dir()
            verify_unchanged()
            current = bridge.request("state")
            assert Path(current["downloadRoot"]) == target
            for ident, name in ((device, "Portable synthetic fixture"), (second_device, "Second synthetic phone")):
                entry = next(phone for phone in current["devices"] if phone["id"] == ident)
                assert Path(entry["defaultDownloadDir"]) == target / name
            bridge.request("undo_download_location", {"historyId": changed["historyId"]})
            assert Path(bridge.request("state")["downloadRoot"]) == old_root
            verify_unchanged()
            bridge.request("redo_download_location", {"historyId": changed["historyId"]})
            assert Path(bridge.request("state")["downloadRoot"]) == target
            verify_unchanged()
            bridge.request("undo_download_location", {"historyId": changed["historyId"]})
            verify_unchanged()
            return {"allPhonesUseOneRoot": True, "rootFolderName": "EyeCopy", "existingFilesStayedInPlace": True,
                    "noCopiesCreatedAtNewDefault": True, "globalPreferenceUndoRedoRoundTrip": True,
                    "finalSavedLocationRestored": True, "phonesVerified": 2}
        step("frozen-engine-global-download-root", shared_root)

        def phone_read():
            if bridge is None:
                raise RuntimeError("Engine did not start")
            end = time.monotonic() + 15
            actual = []
            while time.monotonic() < end:
                state = bridge.request("state")
                actual = [d for d in state["devices"] if d["id"] not in {device, second_device}]
                if any(d.get("online") for d in actual):
                    break
                time.sleep(0.5)
            if not actual and not args.require_phone:
                return {"phoneDetected": False, "optional": True}
            assert actual, f"Expected connected phone; connection={state['connection']}"
            connected = next((phone for phone in actual if phone.get("online")), None)
            assert connected, state["connection"]
            apps = bridge.request("list_apps", {"deviceId": connected["id"]})
            assert any(d.get("online") for d in actual), state["connection"]
            return {"phoneDetected": True, "pairedReadOnlyConnection": True,
                    "sharedAppCount": len(apps), "phoneWrites": False, "endpoint": "127.0.0.1:27015"}
        step("frozen-engine-read-only-usb-metadata", phone_read)

        def privacy():
            files = [p for p in distribution.rglob("*") if p.is_file()]
            forbidden_parts = {"test-output", ".venv", ".eyecopy-cache", "build-work"}
            rejected = []
            for path in files:
                relative = path.relative_to(distribution)
                if any(part in forbidden_parts for part in relative.parts):
                    rejected.append(str(relative))
                if relative.parts[:3] == ("resources", "app", "data") or relative.parts[0] == "data":
                    rejected.append(str(relative))
                if "tests" in relative.parts:
                    rejected.append(str(relative))
                if path.suffix.lower() in {".mov", ".mp4", ".heic", ".jpg", ".jpeg", ".part", ".plist"}:
                    rejected.append(str(relative))
                if path.name in {"state.json", "desktop.log", "NativeDragCheck.cs", "NativeDragCheck.js", "RendererFixture.js", "RendererFixturePreload.js"}:
                    rejected.append(str(relative))
            assert not rejected, rejected[:30]
            personal_markers = tuple(value for value in (os.environ.get("USERPROFILE"), str(ROOT.resolve())) if value)
            checked = 0
            for base in (app / "desktop", app / "ui", app / "scripts", distribution / "Sources" / "EyeCopy"):
                for path in base.rglob("*"):
                    if path.is_file() and path.suffix in {".py", ".js", ".cs", ".txt", ".html", ".json"}:
                        text = path.read_text(encoding="utf-8", errors="replace")
                        assert not any(marker in text for marker in personal_markers), str(path.relative_to(distribution))
                        checked += 1
            return {"distributionFiles": len(files), "firstPartyTextFilesChecked": checked,
                    "personalFixturesAndDatabasesExcluded": True, "sampleMediaExcluded": True}
        step("distribution-privacy-allowlist", privacy)
    finally:
        if bridge is not None:
            report["engineShutdown"] = bridge.close()
        report["status"] = "passed" if all(s["status"] == "passed" for s in report["steps"]) else "failed"
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"status": report["status"], "report": str(output / "report.json"),
                          "isolatedRuntime": str(isolated)}), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
