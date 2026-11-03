Unicode True
Name "EyeCopy"
OutFile "${OUTPUT_EXE}"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\EyeCopy"
SetCompressor /SOLID lzma
SetCompressorDictSize 64
Icon "${APP_ICON}"
UninstallIcon "${APP_ICON}"
VIProductVersion "${WIN_VERSION}"
VIAddVersionKey /LANG=1033 "ProductName" "EyeCopy"
VIAddVersionKey /LANG=1033 "FileDescription" "EyeCopy Setup"
VIAddVersionKey /LANG=1033 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "EyeCopy contributors"
!define BUILD_SETUP
!include "UninstallCore.nsh"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${APP_LICENSE}"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  SetShellVarContext current
  SetRegView 64
  ${GetParameters} $R0
  ${GetOptions} $R0 "/EYC_TESTROOT=" $TestRoot
  StrCpy $CleanupArgs ""
  ${If} $TestRoot != ""
    StrCpy $CleanupArgs '--test-root "$TestRoot"'
  ${EndIf}
FunctionEnd

Section "EyeCopy" SEC_MAIN
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=ProfileCleanup.exe "${CLEANUP_HELPER}"
  File /oname=incoming-manifest.json "${APP_DIRECTORY}\.eyecopy-build.json"
  nsExec::ExecToStack '"$PLUGINSDIR\ProfileCleanup.exe" --app-root "$INSTDIR" --incoming-manifest "$PLUGINSDIR\incoming-manifest.json" --validate-install $CleanupArgs'
  Pop $R0
  Pop $R1
  ${If} $R0 != 0
    MessageBox MB_ICONSTOP|MB_OK "Choose a dedicated empty EyeCopy folder, or an existing EyeCopy installation.$\r$\n$\r$\n$R1" /SD IDOK
    SetErrorLevel 2
    Abort
  ${EndIf}
  SetOutPath "$INSTDIR"
  File /r "${APP_DIRECTORY}\*.*"
  ${If} $TestRoot == ""
    CreateDirectory "$SMPROGRAMS\EyeCopy"
    IfFileExists "$SMPROGRAMS\EyeCopy\EyeCopy.lnk" +2 0
      CreateShortcut "$SMPROGRAMS\EyeCopy\EyeCopy.lnk" "$INSTDIR\EyeCopy.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\EyeCopy" "DisplayName" "EyeCopy"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\EyeCopy" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\EyeCopy" "Publisher" "EyeCopy contributors"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\EyeCopy" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\EyeCopy" "DisplayIcon" "$INSTDIR\EyeCopy.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\EyeCopy" "UninstallString" '$\"$INSTDIR\UninstallEyeCopy.exe$\"'
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\EyeCopy" "QuietUninstallString" '$\"$INSTDIR\UninstallEyeCopy.exe$\" /S'
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\EyeCopy" "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\EyeCopy" "NoRepair" 1
  ${EndIf}
SectionEnd
