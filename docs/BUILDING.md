# Build and test EyeCopy

Use a packaged release for ordinary installation. These instructions are for contributors building from source.

## Prerequisites

- Windows x64 with Python 3.10 x64 and Node.js 24 LTS (including npm) installed.
- A source checkout or the **EyeCopy-source.zip** release asset.
- Internet access for pinned dependencies and the first media-tool build.
- Compatible Apple USB support and a trusted device only when testing device access.

Build from a writable local path. The Windows .NET Framework C# compiler creates the drag helpers; the build locates it under the Windows installation. Builds do not change Apple driver bindings or install a replacement transport service.

## Install dependencies

Run these commands in PowerShell from the repository root:

```powershell
py -3.10 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt -r requirements-build.txt
npm ci
npm run setup:media
```

The lock files pin the application dependencies. `setup:media` builds the preview tools needed for source playback and renderer fixtures. On its first run, it downloads an isolated compiler/toolchain and matching media sources into `build-work\media`, then compiles the required FFmpeg components. This can take several minutes. It does not install a system compiler or change device drivers. Matching sources and build manifests are kept in `LICENSES`.

Do not commit `.venv`, `node_modules`, build output, device profiles or local test output.

## Run from source

```powershell
npm start
```

The source application needs the installed development dependencies. The portable package contains its own runtimes. Source runs use the ignored `data` folder in the checkout by default. Set `EYECOPY_DATA_DIR` to use an isolated test profile:

```powershell
$env:EYECOPY_DATA_DIR = Join-Path (Get-Location) 'test-output\manual-profile'
npm start
```

Use synthetic files and dedicated folders for deletion, cancellation and recovery checks. Do not run destructive tests against personal downloads or device libraries.

## Run checks

```powershell
npm run check
.venv\Scripts\python.exe -m unittest discover -s backend -p test_*.py
node --test tests\*.test.js
node_modules\electron\dist\electron.exe scripts\RendererFixture.js
node_modules\electron\dist\electron.exe scripts\RendererOfflineThumbnailsFixture.js
```

Some integration checks depend on Windows native services or device support; inspect skipped checks and fixture output. Renderer fixtures use isolated synthetic media and profiles.

## Configure a release

Set the application version and public GitHub repository before building a distribution. The repository is the source used for update checks; it must be the repository where you publish the Windows release assets. A source upload alone does not provide an installable update.

Replace the repository placeholder with the public release repository:

```powershell
.venv\Scripts\python.exe scripts\ConfigureRelease.py --version 1.0.0 --repository https://github.com/OWNER/REPOSITORY
```

For a later release, use its new three-part version, such as `1.0.1`. Rebuild the application after changing that configuration. Do not embed a GitHub access token or private repository credentials in the application. See [publishing instructions](PUBLISHING.md) for the matching tag and assets.

## Build a portable package

```powershell
.venv\Scripts\python.exe scripts\BuildPortable.py
```

The complete runnable folder is **release\EyeCopy**. Keep its contents together. The build freezes the transfer engine, bundles application runtimes and preview tools, compiles native helpers, and includes source and third-party notices. It invokes the media builder automatically; a separate `setup:media` run is unnecessary when building the portable package directly.

The build also generates **UninstallEyeCopy.exe** and **UpdateEyeCopy.exe** for the portable folder. On first use, the installer builder downloads the pinned NSIS compiler into the ignored **build-work\installer** folder. It does not install NSIS globally. To generate just the uninstaller during development:

```powershell
.venv\Scripts\python.exe build-support\BuildInstaller.py --uninstaller-only
```

First-time builds require network access and may take several minutes. People installing the resulting application do not need a compiler, Python or Node.js.

## Build Windows setup

After the portable folder has been built, generate the per-user setup program:

```powershell
.venv\Scripts\python.exe build-support\BuildInstaller.py --setup
```

This writes **artifacts\Setup-EyeCopy-1.0.0.exe**. The setup program installs the same application files, adds a Windows Installed apps entry and includes uninstall support. Uninstall clears verified EyeCopy profile data while preserving downloaded originals. Use isolated synthetic profiles for cleanup tests.

Run the package smoke check:

```powershell
.venv\Scripts\python.exe scripts\TestPortable.py
```

This check uses an isolated runtime and synthetic PC files. It can perform read-only device checks when a device is available; it does not upload or delete device files.

Prepare distribution archives and checksums:

```powershell
.venv\Scripts\python.exe scripts\PrepareRelease.py
```

The release assets include **artifacts\Setup-EyeCopy-1.0.0.exe**, **EyeCopy-Windows-x64.zip**, **EyeCopy-source.zip**, **EyeCopy-dependency-sources.zip** and **SHA256SUMS.txt**. Keep binaries and generated archives out of Git history; attach them to a GitHub Release. The updater needs the versioned setup executable and its matching checksum file attached to the latest stable release. Review validation results, source availability and dependency notices before publishing. See [publishing instructions](PUBLISHING.md).
