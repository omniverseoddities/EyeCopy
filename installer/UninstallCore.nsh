!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
Var TestRoot
Var CleanupArgs
!ifndef BUILD_SETUP
Var CleanupReport
!define MUI_UNCONFIRMPAGE_TEXT_TOP "Remove EyeCopy and its registered settings, saved devices, transfer history and cached previews. Downloaded photos and videos will be kept. Quit EyeCopy from its notification-area menu before continuing."
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

Function un.onInit
  SetShellVarContext current
  SetRegView 64
  ${GetParameters} $R0
  ${GetOptions} $R0 "/EYC_TESTROOT=" $TestRoot
  StrCpy $CleanupArgs ""
  ${If} $TestRoot != ""
    StrCpy $CleanupArgs '--test-root "$TestRoot"'
  ${EndIf}
FunctionEnd

Section "Uninstall"
  InitPluginsDir
  StrCpy $CleanupReport "$PLUGINSDIR\cleanup-result.json"
  ${If} $TestRoot != ""
    StrCpy $CleanupReport "$TestRoot\cleanup-result.json"
  ${EndIf}
  SetOutPath "$PLUGINSDIR"
  File /oname=ProfileCleanup.exe "${CLEANUP_HELPER}"
  nsExec::ExecToStack '"$PLUGINSDIR\ProfileCleanup.exe" --app-root "$INSTDIR" --cleanup --confirmed --report "$CleanupReport" $CleanupArgs'
  Pop $R0
  Pop $R1
  DetailPrint "$R1"
  ${If} $R0 != 0
    MessageBox MB_ICONSTOP|MB_OK "EyeCopy could not finish uninstalling. Close EyeCopy and review the details, then try again.$\r$\n$\r$\n$R1" /SD IDOK
    SetErrorLevel 2
    Abort
  ${EndIf}
  ${If} $TestRoot == ""
    RMDir "$SMPROGRAMS\EyeCopy"
    ReadRegStr $R0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\EyeCopy" "InstallLocation"
    ${If} $R0 == $INSTDIR
      DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\EyeCopy"
    ${EndIf}
  ${EndIf}
  Delete "$INSTDIR\UninstallEyeCopy.exe"
  RMDir "$INSTDIR"
  DetailPrint "Downloaded originals and unrecognized files were kept. Any remaining folders can be reviewed in File Explorer."
  SetErrorLevel 0
SectionEnd
!endif
