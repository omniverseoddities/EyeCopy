EyeCopy — third-party notices and source materials

See SOURCE-NOTICE.txt and LICENSE at the distribution root for application terms
and the bundled components. Upstream notices are preserved without modification.

python-package-inventory.json identifies installed dependency versions, declared
licenses and copied notice files. Build-only dependencies can appear in this
inventory even when omitted from the runtime.

source-archive-inventory.json records exact archive locations, upstream URLs,
versions and SHA-256 hashes. Publisher checksum and signature checks are marked
explicitly. Sources here are not executed or extracted during application use.

FFmpeg-provenance.json links the bundled executable to its exact source archives,
build recipe and toolchain inputs. FFmpeg-source-verification.json records the
official release's valid PGP signature and source-content comparison. The media
recipe is supplied in Sources/EyeCopy/build-support/media in the portable package
and build-support/media in the source repository.

HEIF-source-provenance.json identifies the native HEIF-library source archives,
matching exported versions, wheel workflow and MSYS2 recipes under native-build.
The x265 archive's version file matches the actual Windows encoder metadata.
The original upstream wheel notice remains unchanged.

pymobile-source-verification.json compares installed Python source files with the
pinned upstream archive. The generated wheel version file is under source-overlays.

GPL-3.0.txt, GPL-2.0.txt, LGPL-3.0.txt and LGPL-2.1.txt provide full license texts.
Electron-MIT.txt, Koffi-MIT.txt and Python-3.10.11-license.txt identify those runtime
components. Chromium notices remain beside EyeCopy.exe.

CollectNotices.py collects metadata and required notices from the build environment.
It does not inspect application profiles, device identifiers, pairing records or media.
