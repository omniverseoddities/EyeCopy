EyeCopy for Windows

Browse and transfer iPhone and iPad photos, videos and shared app documents.

INSTALL
Download Setup-EyeCopy-1.0.0.exe from the repository's Releases page and follow
the setup steps. EyeCopy installs for your Windows account and adds an uninstall
entry. For portable use, download EyeCopy-Windows-x64.zip, extract its complete
EyeCopy folder and open EyeCopy.exe. Keep all supporting files together.
GitHub's Source code download needs a developer setup.

The package targets Windows 10/11 on Intel/AMD x64. It includes application
runtimes, preview tools and drag helpers. No Python, Node.js or compiler is needed.

USB access requires compatible Apple USB support. If missing, install Apple
Devices from the Microsoft Store, open it once, connect an unlocked device,
and approve Trust on the device when asked. The Apple Devices window can stay
closed after setup; its background transport must remain available.
  Apple Devices: https://apps.microsoft.com/detail/9NP83LWLPZ9K
  Apple setup help: https://support.apple.com/en-us/118290

Use a USB 3 data cable and compatible port for a USB 3 connection. Advertised
port speed does not guarantee file-copy speed; the queue shows measured MB/s.

GET STARTED
Photos App browses camera originals. Files App browses documents exposed by
participating apps through USB File Sharing. This does not include every Files
location, iCloud Drive, Safari Downloads or private app storage.

Downloads start in Videos\EyeCopy\device name. The folder icon beside a device
opens its downloads. Settings -> Change default folder changes the shared
location for future downloads without moving existing files.

Cached thumbnails and downloaded originals remain available offline. Streaming
from a device, uploads and new device downloads require a connection.

UPDATES
Use Settings to check for a newer release and choose automatic update checks.
UpdateEyeCopy.exe opens the same controls. The publisher's GitHub release
repository must be configured; enter it in Settings if the package does not
supply one. Automatic checks are enabled when a repository is configured and
can be turned off. Installation asks for confirmation.
Updates preserve settings, preview caches and downloaded originals. The updater
needs a published Windows setup asset and checksum, not only a source upload.

UNINSTALL
Quit EyeCopy from its notification-area menu first.
Use EyeCopy's entry in Windows Installed apps, or run UninstallEyeCopy.exe from
the application folder. The uninstaller clears known data in verified EyeCopy
profiles, including settings, catalogues, transfer history and preview caches.
Downloaded photos and videos remain in their saved folders. Shared Apple USB support stays
installed. A path that cannot be verified as EyeCopy-owned is left for review.

Read QUICK START.txt for installation, GUIDE.html for operation, and Transport
Options.html for connection details. The source repository includes docs/BUILDING.md
and docs/PRIVACY.md. Third-party notices and corresponding-source information are
in SOURCE-NOTICE.txt and LICENSES.
