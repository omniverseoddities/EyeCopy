"""Collect dependency notices and pinned upstream source archives for EyeCopy.

This build utility reads installed package metadata, not application profiles.
Archives are preserved unmodified; no downloaded code is executed or extracted.
"""
from pathlib import Path
from importlib import metadata
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import urllib.error

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SOURCES = HERE / "sources"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def fetch(url, name, expected=None):
    target = SOURCES / name
    SOURCES.mkdir(exist_ok=True)
    if target.exists() and (expected is None or digest(target) == expected):
        return {"file": "sources/" + name, "url": url, "bytes": target.stat().st_size,
                "sha256": digest(target), "verifiedAgainstPublisherSha256": expected is not None}
    temporary = target.with_name(target.name + ".download")
    try:
        with urllib.request.urlopen(url, timeout=45) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
    except urllib.error.URLError:
        # Use Windows' trusted certificate store without disabling TLS checks.
        subprocess.run(["curl.exe", "--fail", "--location", "--retry", "2", "--connect-timeout", "30",
                        "--max-time", "300", "--output", str(temporary), url], check=True)
    if expected and digest(temporary) != expected:
        temporary.unlink()
        raise RuntimeError("Upstream archive checksum does not match: " + name)
    temporary.replace(target)
    return {"file": "sources/" + name, "url": url, "bytes": target.stat().st_size,
            "sha256": digest(target), "verifiedAgainstPublisherSha256": expected is not None}


def pinned_sdist(name, version):
    inventory = HERE / "source-archive-inventory.json"
    if inventory.is_file():
        for record in json.loads(inventory.read_text(encoding="utf-8"))["archives"]:
            if record.get("component", "").lower().replace("_", "-") == name.lower().replace("_", "-") and record.get("version") == version:
                fetch(record["url"], Path(record["file"]).name, record["sha256"])
                return record
    url = "https://pypi.org/pypi/" + name + "/" + version + "/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            info = json.load(response)
    except urllib.error.URLError:
        temporary = SOURCES / (name + ".metadata.download")
        subprocess.run(["curl.exe", "--fail", "--location", "--connect-timeout", "30", "--max-time", "60",
                        "--output", str(temporary), url], check=True)
        info = json.loads(temporary.read_text(encoding="utf-8"))
        temporary.unlink()
    entries = [entry for entry in info["urls"] if entry["packagetype"] == "sdist"]
    if len(entries) != 1:
        raise RuntimeError("Expected exactly one pinned source archive: " + name)
    entry = entries[0]
    result = fetch(entry["url"], entry["filename"], entry["digests"]["sha256"])
    result.update({"component": name, "version": version, "metadataUrl": url})
    return result


def main():
    HERE.mkdir(exist_ok=True)
    # Source archives are release assets, rather than Git blobs. A clean clone
    # restores the exact pinned inputs before a portable package is assembled.
    source_inventory = json.loads((HERE / "source-archive-inventory.json").read_text(encoding="utf-8"))
    sources = source_inventory["archives"]
    for source in sources:
        fetch(source["url"], Path(source["file"]).name, source["sha256"])
    records = []
    for distribution in sorted(metadata.distributions(), key=lambda value: value.metadata["Name"].lower()):
        name, version = distribution.metadata["Name"], distribution.version
        destination = HERE / "python-packages" / (name + "-" + version)
        copied = []
        for relative in distribution.files or []:
            filename = Path(str(relative)).name.upper()
            if not filename.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
                continue
            source = Path(distribution.locate_file(relative))
            if not source.is_file():
                continue
            # Preserve nested license names while omitting installation paths.
            parts = Path(str(relative)).parts
            metadata_index = next((i for i, part in enumerate(parts) if part.endswith((".dist-info", ".egg-info"))), None)
            suffix = Path(*parts[metadata_index + 1:]) if metadata_index is not None else Path("package", *[part for part in parts if part not in ("..", ".")])
            target = destination / suffix
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(str(target.relative_to(HERE)).replace("\\", "/"))
        records.append({"name": name, "version": version,
                        "licenseExpression": distribution.metadata.get("License-Expression"),
                        "declaredLicense": distribution.metadata.get("License"),
                        "projectUrls": distribution.metadata.get_all("Project-URL") or [],
                        "licenseFiles": copied})
    (HERE / "python-package-inventory.json").write_text(json.dumps({
        "scope": "Pinned build environment dependency notice inventory; build-only packages may be listed even when omitted from the frozen runtime.",
        "packages": records}, indent=2), encoding="utf-8")

    for origin, target in ((ROOT / "node_modules/koffi/LICENSE.txt", HERE / "Koffi-MIT.txt"),
                          (ROOT / "node_modules/electron/dist/LICENSE", HERE / "Electron-MIT.txt")):
        shutil.copy2(origin, target)
    gpl = metadata.distribution("pymobiledevice3").locate_file("pymobiledevice3-7.6.0.dist-info/licenses/LICENSE")
    shutil.copy2(gpl, HERE / "GPL-3.0.txt")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, HERE / "Python-3.10.11-license.txt")

    pymobile_source = pinned_sdist("pymobiledevice3", "7.6.0")
    if metadata.version("pymobiledevice3") != "7.6.0":
        raise RuntimeError("This source comparison requires the pinned pymobiledevice3 7.6.0 wheel.")
    wheel_overlays = []
    compared = 0
    with tarfile.open(HERE / pymobile_source["file"], "r:gz") as upstream:
        prefix = "pymobiledevice3-7.6.0/"
        for member in upstream.getmembers():
            if not member.isfile() or not member.name.startswith(prefix + "pymobiledevice3/") or not member.name.endswith(".py"):
                continue
            relative = member.name[len(prefix):]
            installed = Path(metadata.distribution("pymobiledevice3").locate_file(relative))
            if not installed.is_file():
                continue
            compared += 1
            data = upstream.extractfile(member).read()
            if installed.read_bytes() != data:
                if relative != "pymobiledevice3/_version.py":
                    raise RuntimeError("Unexpected source/wheel difference: " + relative)
                destination = HERE / "source-overlays" / "pymobiledevice3-7.6.0" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(installed, destination)
                wheel_overlays.append({"file": str(destination.relative_to(HERE)).replace("\\", "/"),
                                       "sha256": digest(destination)})
    (HERE / "pymobile-source-verification.json").write_text(json.dumps({
        "version": "7.6.0", "installedPythonSourceFilesCompared": compared,
        "sourceArchive": pymobile_source["file"], "wheelSourceOverlays": wheel_overlays,
        "explanation": "The published sdist and installed wheel differ only in generated _version.py commit metadata (sdist g63c166600; wheel None). The installed generated file is supplied as an overlay; the upstream archive remains unmodified."}, indent=2), encoding="utf-8")
    pinned_sdist("pillow-heif", "1.8.0")
    for record in records:
        if not record["licenseFiles"]:
            sources.append(pinned_sdist(record["name"], record["version"]))
    ffmpeg = ROOT / "vendor/ffmpeg/ffmpeg.exe"
    provenance = json.loads((HERE / "FFmpeg-provenance.json").read_text(encoding="utf-8"))
    if not provenance.get("completeCorrespondingSourceConfirmed") or provenance["binarySha256"] != digest(ffmpeg):
        raise RuntimeError("Build the source-matched media executable before collecting release notices.")
    sources = list({record["file"]: record for record in sources}.values())
    (HERE / "source-archive-inventory.json").write_text(json.dumps({"archives": sources}, indent=2), encoding="utf-8")
    print(json.dumps({"packages": len(records), "licenseFiles": sum(len(record["licenseFiles"]) for record in records),
                      "archives": [{"file": item["file"], "bytes": item["bytes"], "sha256": item["sha256"]} for item in sources]}))


if __name__ == "__main__":
    main()
