Unicode True
Name "EyeCopy"
UninstallCaption "Uninstall EyeCopy"
OutFile "${OUTPUT_EXE}"
RequestExecutionLevel user
SilentInstall silent
SetCompressor /SOLID lzma
Icon "${APP_ICON}"
UninstallIcon "${APP_ICON}"
VIProductVersion "${WIN_VERSION}"
VIAddVersionKey /LANG=1033 "ProductName" "EyeCopy"
VIAddVersionKey /LANG=1033 "FileDescription" "Uninstall EyeCopy"
VIAddVersionKey /LANG=1033 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "EyeCopy contributors"
!include "UninstallCore.nsh"
!insertmacro MUI_LANGUAGE "English"
Section
  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\UninstallEyeCopy.exe"
SectionEnd
