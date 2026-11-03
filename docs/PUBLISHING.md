# Publish EyeCopy on GitHub

The release label is **v1.0.0** and the application version is **1.0.0**.

## Repository

Create a repository in your GitHub account. Upload the source project, including
the README, source folders, lock files, license, documentation and workflow.
Do not upload local profiles, `.venv`, `node_modules`, build folders or `artifacts`.
The included `.gitignore` excludes these generated and private directories.

For website uploads, extract **EyeCopy-source.zip** into a new empty folder and
upload that folder's contents. This is the clean public source export. Avoid
uploading the development workspace as a folder: the website does not apply its
`.gitignore` rules to files you select manually.

The prepared **artifacts/GitHub-source** folder contains the same clean source
files and can also be used for a website upload. Keep the **tests** folder in
the public repository: the checks and contributor instructions use it. Keep
**test-output** private; generated diagnostics and fixtures are local-only.

For a Git workflow, set your own commit identity and connect your repository
before committing and pushing. Do not force-add ignored files. Keep executables
and dependency archives out of Git history.

For the command-line route, install Git for Windows and create an empty GitHub
repository named **EyeCopy**. Open PowerShell in this source folder, then run the
commands below. Replace the uppercase placeholders with your GitHub username,
GitHub no-reply email address and repository URL. Your commit identity is public.

```powershell
git config user.name "YOUR GITHUB USERNAME"
git config user.email "YOUR GITHUB NO-REPLY EMAIL"
git add .
git commit -m "EyeCopy v1.0.0"
git remote add origin https://github.com/YOUR-USERNAME/EyeCopy.git
git push -u origin main
```

An exported source folder may need `git init -b main` before these commands.
GitHub may ask you to sign in when you push. Keep credentials out of commands
and source files.

The Windows checks workflow tests changes automatically after the source is
pushed. It does not create or publish a release.

## Configure updates

EyeCopy checks the GitHub repository configured by its publisher. This release
is configured for **https://github.com/omniverseoddities/EyeCopy**. Users can
override the release repository in Settings. Update checks use public GitHub
Releases; private repositories require manual downloads while under review.
For a fork, configure your own public repository URL before building the release,
without credentials or an access token.

From the source folder, run this command with your repository URL:

```powershell
.venv\Scripts\python.exe scripts\ConfigureRelease.py --version 1.0.0 --repository https://github.com/OWNER/REPOSITORY
```

This sets the application version and the repository bundled into the updater.
For a later release, change `--version` to the new three-part version, then
rebuild with **BuildPortable.py** and prepare the assets with
**PrepareRelease.py**. For example, a **1.0.1** build uses tag **v1.0.1** and setup
asset **Setup-EyeCopy-1.0.1.exe**. The application, setup, release tag and checksum
must all describe the same build. Editing the README or uploading new source
files does not update the application binaries.

## Downloadable release

Build and validate the package using [the build instructions](BUILDING.md), then
run `.venv\Scripts\python.exe scripts\PrepareRelease.py`. Review
`artifacts/verification.json` and verify the checksums in
`artifacts/SHA256SUMS.txt`.

On the repository's Releases page, create a draft with tag **v1.0.0** and title
**EyeCopy v1.0.0**. Attach these generated assets:

- `EyeCopy-Windows-x64.zip`: the complete portable Windows application.
- `Setup-EyeCopy-1.0.0.exe`: per-user Windows setup with uninstall support.
- `EyeCopy-source.zip`: the authored source, tests and documentation.
- `EyeCopy-dependency-sources.zip`: matching third-party source and notices.
- `SHA256SUMS.txt`: SHA-256 checksums for the release assets, including setup.

State that Windows x64 and compatible Apple USB support are required, and link to
the installation guide. Users can choose setup or the portable ZIP. Both include
uninstall support that preserves downloaded photos and videos. GitHub's
automatically generated Source code archive is for developers.

Publish the matching tag as a stable release, rather than a draft or prerelease,
when it is ready for update checks. Keep the versioned setup executable and its
matching **SHA256SUMS.txt** attached to that release. EyeCopy compares the release
version, downloads the Windows setup asset and verifies its checksum before
asking the user to install. Updating preserves the local profile and downloads.

Uploading the source repository alone does not publish an installable update.
For every release, configure the version, build the application, run
**PrepareRelease.py**, review the output, and attach the generated setup and
checksum along with the other assets before publishing the stable tag.

The executable is unsigned. Do not describe it as signed, Microsoft-certified,
tested on every Windows installation, or capable of guaranteed transfer rates.
Review the draft and its attachments before publishing it.
