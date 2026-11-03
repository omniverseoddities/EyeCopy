"""Verify folder deletion using only new disposable app-document fixtures."""
from pathlib import Path
import argparse
import hashlib
import json
import socket
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.engine import Engine
from backend.afc_io import read as afc_read


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Create and delete this test's own remote fixture only")
    args = parser.parse_args()
    if not args.execute:
        print("No phone changes. Pass --execute only for the authorized disposable-folder test.")
        return
    socket.setdefaulttimeout(12)
    test = ROOT / "test-output" / "phone-folder-live" / uuid.uuid4().hex
    engine = Engine(test / "data", start=False, download_dir=test / "downloads")
    report = {"syntheticOnly": True, "otherPhoneFilesTouched": False}
    app = "com.blackmagic-design.DaVinciCamera"
    try:
        engine.poll_devices()
        phones = [device for device in engine.state()["devices"] if device.get("online")]
        assert len(phones) == 1, "One unlocked paired phone is required for the isolated test."
        phone = phones[0]["id"]
        apps = engine.list_apps(phone)
        assert any(provider["id"] == app for provider in apps), "The expected shared test app is unavailable."
        remote = "/Documents/EyeCopy/_EyeCopy_folder_check_" + uuid.uuid4().hex
        payloads = {remote + "/one.txt": b"EyeCopy disposable folder fixture one.\n",
                    remote + "/nested/two.txt": b"EyeCopy disposable folder fixture two.\n"}
        with engine._afc(phone, "app", app) as afc:
            engine._check_directory(afc, "/Documents/EyeCopy", "app")
            assert not afc.exists(remote)
            afc.makedirs(remote + "/nested")
            for path, contents in payloads.items():
                assert not afc.exists(path)
                handle = afc.fopen(path, "w")
                try:
                    afc.fwrite(handle, contents)
                finally:
                    afc.fclose(handle)
                handle = afc.fopen(path, "r")
                try:
                    assert afc_read(afc, handle, len(contents)) == contents
                finally:
                    afc.fclose(handle)
            item = engine._catalogue_item(phone, "app", app, remote, afc.stat(remote))
        local_copy = test / "PC-copy-one.txt"
        local_copy.write_bytes(payloads[remote + "/one.txt"])
        digest = hashlib.sha256(local_copy.read_bytes()).hexdigest()
        scope = engine.scope(phone, "app", app, "/Documents/EyeCopy")
        engine.current_scope = scope
        engine._catalogues[scope] = [item]
        plan = engine.handle("preview_phone_folder_delete", {"items": [item]})
        assert plan["files"][0]["containedFiles"] == 2
        assert plan["files"][0]["containedFolders"] == 2
        assert "Undo" in plan["warning"] and plan["permanent"]
        outcome = engine.handle("confirm_phone_folder_delete", {"token": plan["token"]})
        assert outcome["deleted"] == 1 and outcome["failed"] == 0, outcome
        with engine._afc(phone, "app", app) as afc:
            assert not afc.exists(remote) and afc.exists("/Documents/EyeCopy")
        assert hashlib.sha256(local_copy.read_bytes()).hexdigest() == digest
        report.update(passed=True, createdFiles=2, removedFolders=2, removedFiles=2,
                      unchangedPCcopy=True, rootAppFolderPreserved=True, warningReviewed=True)
    finally:
        engine.close()
        test.mkdir(parents=True, exist_ok=True)
        (test / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report": str(test / "report.json")}))


if __name__ == "__main__":
    main()
