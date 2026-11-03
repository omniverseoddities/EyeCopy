# Privacy and local storage

EyeCopy uses a local desktop interface and USB device services. Its catalogue, settings and previews are stored on the PC. No EyeCopy account or cloud storage is required for transfers.

## Data on the PC

The application's normal profile is **%LOCALAPPDATA%\EyeCopy**. It can contain device names, saved catalogues, source filenames and paths, transfer history, download records, cached media previews, connection diagnostics and application logs. Source runs use the ignored `data` folder in the checkout by default. The `EYECOPY_DATA_DIR` environment variable can select another dedicated profile folder.

EyeCopy records its profile locations for uninstall cleanup and writes an ownership marker in each eligible profile. Use a dedicated folder for a custom profile. Do not point `EYECOPY_DATA_DIR` at a folder containing unrelated documents or another application's data.

Original downloads are separate, initially in **Videos\EyeCopy\device name**, or in the location selected by the user. Changing the default location does not move existing downloads. Removing the extracted application folder does not remove its profile or downloaded files.

Cached previews allow offline browsing. **Clear cache** removes cached browsing data for the selected device; it is separate from deleting downloaded originals. Reconnect and reload the catalogue when new previews are needed.

## Remove EyeCopy data

Use EyeCopy's Windows **Installed apps** entry or **UninstallEyeCopy.exe** in its application folder. The uninstaller clears known data in verified EyeCopy profiles, including the default profile and registered dedicated custom locations. Settings, catalogues, history, cached previews and download tracking are cleared. Downloaded photos and videos stay in their saved folders.

Cleanup verifies profile ownership and refuses unsafe or unverified paths. Unrelated files, modified application files, locked items and locations that cannot be safely removed are reported for manual review. Folders containing retained files can remain. Unregistered custom locations need manual review. Shared Apple USB software, pairing records and other applications' profiles are outside EyeCopy's cleanup.

See [installation and removal](INSTALLATION.md) for the steps. Deleting the extracted application folder by hand does not perform profile cleanup.

## Device permissions and connections

Apple's installed USB support manages the device transport and Trust relationship. EyeCopy does not distribute Apple pairing records or require users to share them. Access remains subject to iOS/iPadOS permissions and the folders exposed by participating apps.

Video previews use a temporary local media endpoint. EyeCopy is not a public file-sharing server. Guide links open support sites in the user's browser; those sites have their own privacy policies.

## Update checks

When you check for updates, EyeCopy contacts the configured GitHub repository to read its latest stable release information. Automatic checks can be controlled in Settings. Downloading an update retrieves its Windows setup asset and checksum from that release. GitHub receives the network request, including your IP address; EyeCopy does not send your device catalogue, filenames, previews or downloaded media as part of an update check.

The repository preference and update-check state are stored in the local profile. Automatic checks are enabled by default when a release repository is configured and can be turned off. If none is configured, EyeCopy makes no update network requests. Installation requires confirmation and preserves the profile and downloaded originals.

## Sharing a bug report

Describe the steps, application version, Windows version and relevant device/software versions. Before sharing a screenshot or diagnostic excerpt, remove:

- Personal filenames, paths, thumbnails and media content.
- Device names, identifiers and network details.
- Account details, pairing material and unrelated system information.

Do not upload a raw application profile, downloaded media library or Apple pairing directory to a public issue. Use a small synthetic file to demonstrate a transfer problem.
