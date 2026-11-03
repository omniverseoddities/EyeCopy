# Bundled media support

EyeCopy's Windows package includes its media executable. Application users do not need a compiler, Python, Node.js or these build tools.

For a source build, install Git for Windows and 7-Zip, then use the project's Python environment:

```powershell
.venv\Scripts\python.exe build-support\media\BuildMedia.py
```

The builder verifies and reuses an existing matching executable. Otherwise it downloads the pinned portable compiler and assembler into `build-work/media`, verifies every input checksum and builds from the source archives listed in `inputs.json`. Missing source archives are restored from their pinned official URLs. The first build can take several minutes. No global compiler installation, driver change or security-setting change is performed.

The build uses FFmpeg's native decoders and formats, x264 for software H.264 encoding, and NVIDIA codec API headers for optional GPU encoding. The finished executable requires only standard Windows DLLs. GPU encoding also requires a compatible installed GPU driver; software encoding remains available.

`--force` rebuilds the source directories. `--resume` continues a configured owned workspace without repeating configuration. On a volume without Windows short-path aliases, use a project path without spaces.

`build.sh` contains the compiler/configuration commands. `prepare-generated.py` supplies the Windows archiver response-file change and removes local paths from generated configuration metadata. Upstream archives remain unmodified. `LICENSES/FFmpeg-provenance.json` records the executable, source and recipe hashes. The official FFmpeg source signature and verification record are supplied under `LICENSES`.

After building, collect notices and all dependency source inputs before packaging:

```powershell
.venv\Scripts\python.exe LICENSES\CollectNotices.py
```

The full application build calls these steps automatically. Toolchains and intermediate output are excluded from the source repository and application package. The application package includes corresponding source materials and required notices.
