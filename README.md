<p align="center"><img src="assets/eyecopy.png" width="96" alt="EyeCopy logo"></p>
<h1 align="center">EyeCopy</h1>
<p align="center">Browse and transfer iPhone and iPad media on Windows.</p>
<p align="center"><strong>v1.0.0 · Windows x64 · USB</strong></p>

**[Download v1.0.0](https://github.com/omniverseoddities/EyeCopy/releases/tag/v1.0.0)** · [Installation guide](docs/INSTALLATION.md) · [Report an issue](https://github.com/omniverseoddities/EyeCopy/issues)

EyeCopy brings camera originals, shared app folders, media previews and transfer progress into one desktop workspace.

> DISCLAIMER: This is completely vibe coded, I wanted to share this because it was a pain transferring large files to my projects and think this can help others, if u wanna support me here's a link: https://www.paypal.biz/OmniverseOddities

![EyeCopy media browser with a synthetic demonstration library](assets/screenshot.png)

*Synthetic demonstration library. Device details and transfer figures are illustrative.*

- Browse photos and videos with thumbnails, sorting, search and Live Photo grouping.
- Download originals, stream supported videos, and play downloaded copies.
- Upload files to participating apps and manage their shared folders.
- Drag originals into File Explorer, with progress, pause, stop and retry controls.
- Keep a separate download folder for each device and browse cached previews offline.

## Install on Windows

1. Open this repository's **Releases** page and download **Setup-EyeCopy-1.0.0.exe**.
2. Run setup and follow its installation steps. EyeCopy installs for your Windows account and adds an uninstall entry.
3. Install [Apple Devices from the Microsoft Store](https://apps.microsoft.com/detail/9NP83LWLPZ9K) if compatible Apple USB support is not already installed. Open it once to complete setup.
4. Open **EyeCopy.exe**, connect an unlocked device with a data-capable USB cable, and approve **Trust** on the device when asked.

EyeCopy targets **Windows 10/11 on Intel/AMD x64**. It includes its application runtimes and preview tools; Python, Node.js and a compiler are not needed. The Apple Devices window can stay closed after setup, but its background USB support must remain available. See the [installation guide](docs/INSTALLATION.md) for setup, updates and troubleshooting.

For portable use, download **EyeCopy-Windows-x64.zip**, extract the complete **EyeCopy** folder and open **EyeCopy.exe**. Keep its supporting files together. GitHub's **Source code** download is for developers.

To remove EyeCopy, quit it from its notification-area menu, then use its Windows **Installed apps** entry or run **UninstallEyeCopy.exe** from its application folder. Uninstall removes verified application files and clears its registered local profile data, including settings, history and preview caches. Downloaded photos and videos stay in their folders. Shared Apple USB support remains installed.

For a USB 3 connection, the device, cable and PC port must all support USB 3. A port's advertised signaling rate is not a guaranteed file-copy speed. EyeCopy shows measured transfer speed in the queue.

## Using EyeCopy

| Workspace | What it provides |
| --- | --- |
| **Photos App** | Camera originals, previews, downloads and supported Live Photo pairs. |
| **Files App** | Documents that installed device apps expose through USB File Sharing, including uploads and folder management. |
| **Transfer queue** | Destination, progress, measured speed, sorting, pause, stop and retry controls. |
| **Settings** | Shared download location, dark color themes, background operation and update checks. |

Downloads start in **Videos\EyeCopy\device name**. The folder icon beside a device opens its downloads. **Settings → Change default folder** changes where future downloads go; it does not move existing files.

**On PC** identifies a verified original in that device's current default folder. Default downloads skip these copies. Dragging to a chosen Explorer folder allows an additional copy. Cached thumbnails load automatically while the device is disconnected; streaming and new device transfers require a connection.

Open **Connection & transfer guide** in the app, or read [GUIDE.html](GUIDE.html), for detailed instructions.

## Updates

Use **Settings** to check for a newer release and choose whether EyeCopy checks automatically. **UpdateEyeCopy.exe** in the application folder opens these controls. Update checks need the publisher's GitHub release repository; if it has not been set in the package, enter it in Settings. With a repository configured, automatic checks are enabled by default and can be turned off. Installation requires your confirmation. Updates preserve settings, cached previews and downloaded originals.

The updater uses the published Windows setup asset and its release checksum. Uploading source files alone does not make an installable update. See the [installation guide](docs/INSTALLATION.md) for update options and [publishing guide](docs/PUBLISHING.md) for release preparation.

## Device access

USB File Sharing exposes participating apps' document folders. It does not expose every location in Files, iCloud Drive, Safari Downloads or private app storage. Uploads use an **EyeCopy** folder inside the selected app's shared Documents folder. The optional device companion in this repository requires separate Apple signing and installation; it is not required for ordinary shared-app transfers.

Deleting shared-app files or folders from the device is permanent and requires confirmation. Photos-library deletion is unavailable. PC cleanup uses the Windows Recycle Bin for eligible tracked downloads and leaves device originals intact.

## Source and support

- [Build and test from source](docs/BUILDING.md)
- [Prepare a GitHub Release](docs/PUBLISHING.md)
- [Privacy and local storage](docs/PRIVACY.md)
- [USB connection details](Transport%20Options.html)
- [Contributing](CONTRIBUTING.md)

Use this repository's Issues page for reproducible bug reports. Remove personal filenames, device identifiers, previews and local paths before sharing diagnostics. EyeCopy is an independent project and is not affiliated with Apple.

See [SOURCE-NOTICE.txt](SOURCE-NOTICE.txt) and the included dependency notices for licensing and corresponding-source information.
