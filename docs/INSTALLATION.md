# Install EyeCopy on Windows

EyeCopy targets Windows 10/11 on Intel/AMD x64. Windows ARM64 and 32-bit Windows are not supported by these packages. You need enough free PC storage for downloads, a data-capable USB cable, and compatible Apple USB support for device access.

## Get the application

1. Open the repository's **Releases** page and download **Setup-EyeCopy-1.0.0.exe**.
2. Run setup and follow its installation steps. The default location is **%LOCALAPPDATA%\Programs\EyeCopy** for your Windows account.
3. Open EyeCopy from the installed shortcut. Setup also creates an entry in Windows **Installed apps** for removal.

For portable use, download **EyeCopy-Windows-x64.zip** instead. In File Explorer, choose **Extract All**, extract to a writable local folder, then open **EyeCopy.exe**. Keep the complete **EyeCopy** folder together. Do not run it from inside the ZIP or copy the EXE away from its supporting files.

The **Source code** downloads contain developer files rather than the runnable application.

The package includes its application runtimes, preview tools and compiled drag helpers. You do not need Python, Node.js or a compiler, and EyeCopy does not install a Windows service.

The application and setup program are unsigned, so Windows may identify their publisher as unknown. Verify the repository and release checksum before deciding whether to run them. Do not disable Windows protection to install EyeCopy. On a managed PC, follow your administrator's software policy.

## Set up the device connection

EyeCopy needs a compatible Apple USB driver and background device transport. If these are missing, install [Apple Devices from the Microsoft Store](https://apps.microsoft.com/detail/9NP83LWLPZ9K) and open it once to complete setup. [Apple's download guidance](https://support.apple.com/en-us/118290) links to the official application.

Connect your unlocked iPhone or iPad with a data-capable cable and approve **Trust** on the device when requested. Existing compatible Apple USB support can be reused. The Apple Devices window may remain closed afterward, but its background support must remain available.

For a USB 3 connection, the device, cable and PC port must all support USB 3. Installing Apple Devices alone does not guarantee a USB 3 link, 10 Gbps throughput or a particular file-copy rate. The queue reports measured transfer speed.

## Check the first transfer

1. In **Your devices**, select the connected device.
2. Open **Photos App** and load its catalogue, or open **Files App** and choose an accessible app directory.
3. Select a small file and choose **Download selected**.
4. Check progress in **Transfer queue**. Open the destination with the folder icon beside the device.

Downloads initially use **Videos\EyeCopy\device name**. **Settings → Change default folder** changes the shared location for future downloads. It does not migrate existing files. Destinations already attached to queued transfers stay with those transfers.

To upload, use **Upload to Files**. On first use, choose a participating File Sharing app. Uploads go into an **EyeCopy** folder inside that app's shared Documents folder. USB access does not expose every location in Files or private app storage.

## Connection troubleshooting

| Symptom | What to check |
| --- | --- |
| Device does not appear | Unlock it, try a direct PC data port, check the cable, and see whether Apple Devices recognizes it. Complete any Trust prompt on the device. |
| Device appears offline | Select the connected device, unlock it and reconnect the cable. The saved catalogue remains available offline. |
| Files folder is missing | Only participating apps' shared Documents folders are accessible. iCloud Drive, Safari Downloads and private containers are outside this USB workflow. |
| Video will not play natively | Try the compatible preview option. Downloaded originals are preserved without transcoding. |
| Transfer is slower than expected | Check the negotiated link in the device panel, cable and port capability, free storage and other device activity. Compare measured queue speed rather than the port's advertised rate. |

Apple provides a [Windows USB connection checklist](https://support.apple.com/en-us/108643?device-type=windows-pc) and [Trust guidance](https://support.apple.com/en-us/109054).

## Background use

**Keep running in the background** is available in Settings. When enabled, closing the window keeps EyeCopy in the notification area; use its eye icon to reopen it or choose **Quit EyeCopy** to stop it. When disabled, closing the window quits the application.

EyeCopy does not register an automatic Windows startup task. A newly connected device can open the window while EyeCopy is already running.

## Updates

Open **Settings** to check for a newer release and control automatic update checks. **UpdateEyeCopy.exe** in the application folder opens the same controls. Checks use the publisher's public GitHub repository. If the package does not include a repository, enter the repository URL in Settings. Automatic checks are enabled by default when a repository is configured; you can turn them off. With no repository configured, EyeCopy makes no update requests.

The repository needs a stable release with a versioned Windows setup executable and matching **SHA256SUMS.txt**. Uploading source files alone does not provide an installable update.

When an update is available, EyeCopy verifies the downloaded setup against its release checksum and asks before installing it. Updates preserve the local profile, including settings, catalogues and cached previews, and leave downloaded photos and videos in their selected locations.

For a manual update, download the setup program from the same repository's Releases page. Quit EyeCopy from its notification-area menu before running setup. For a portable copy, you can instead extract the complete updated portable package into a new folder. Keep all supporting files together. The profile is separate from the application folder, normally in **%LOCALAPPDATA%\EyeCopy**.

## Uninstall and clear local data

1. Quit EyeCopy from its notification-area menu.
2. Open Windows **Settings → Apps → Installed apps** (called **Apps & features** on Windows 10), find **EyeCopy**, and choose **Uninstall**. For a portable copy, open **UninstallEyeCopy.exe** in its application folder.
3. Review the application and profile locations shown by the uninstaller, then confirm removal.

The uninstaller removes verified application files and clears known data in verified local profiles. This includes **%LOCALAPPDATA%\EyeCopy** and registered dedicated custom profiles selected through `EYECOPY_DATA_DIR`. It clears settings, saved devices, catalogues, transfer history, download tracking, preview caches and application logs. A reinstall starts with a fresh catalogue.

**Downloaded photos and videos remain in their saved folders.** Their previous **On PC** tracking is cleared with the profile; the files remain usable in File Explorer. Device originals, shared Apple USB software and other applications' profiles remain in place. Removing EyeCopy does not revoke the device's Windows Trust relationship.

The cleanup checks ownership and folder safety before removing data. Unrelated files, modified application files, locked items and unverified locations are retained and reported for manual review, so their containing folders may remain. Only registered custom profiles that EyeCopy can verify are eligible; review any unlisted locations or reported leftovers before removing them yourself. Deleting only the application folder by hand leaves its separate profile in place; use the uninstaller when you want that data cleared.

If the uninstaller reports that EyeCopy is running, quit it from its notification-area menu and retry. Closing its window can leave it running in the background.
