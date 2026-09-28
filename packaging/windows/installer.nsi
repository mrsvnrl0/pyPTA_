Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!include "FileFunc.nsh"
!ifndef FLAVOR
  !error "Specify /DFLAVOR=Open or /DFLAVOR=Obfuscated"
!endif
!define NAME "pyPTA"
; Retain the installed identity so either edition upgrades the earlier release.
!define KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\AdaptiveCryptoDashboard"
Name "${NAME} (${FLAVOR})"
OutFile "..\release\pyPTA-Setup-1.0.1-${FLAVOR}-x64.exe"
InstallDir "$LOCALAPPDATA\Programs\pyPTA"
InstallDirRegKey HKCU "${KEY}" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "1.0.1.0"
VIAddVersionKey "ProductName" "pyPTA (${FLAVOR})"
VIAddVersionKey "FileDescription" "pyPTA ${FLAVOR} Setup"
VIAddVersionKey "FileVersion" "1.0.1"
VIAddVersionKey "CompanyName" "EternuLL Organisation"
VIAddVersionKey "LegalCopyright" "Copyright 2026 EternuLL Organisation"
!define MUI_ICON "dashboard.ico"
!define MUI_UNICON "dashboard.ico"
!define MUI_WELCOMEPAGE_TEXT "Install pyPTA (${FLAVOR}), including the neural-network model and Python runtime.$\r$\n$\r$\nExisting settings and records are retained. A new profile starts with empty trading data.$\r$\n$\r$\nRequires 64-bit Windows 10 or 11."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\pyPTA.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Launch pyPTA"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "This installer requires 64-bit Windows 10 or 11."
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "Windows 10 or later is required."
    Abort
  ${EndIf}
  SetShellVarContext current
  SetRegView 64
  System::Call 'kernel32::OpenMutexW(i 0x100000, i 0, w "Local\AdaptiveCryptoDashboard.Installed") p.r0'
  ${If} $0 <> 0
    System::Call 'kernel32::CloseHandle(p r0)'
    MessageBox MB_ICONEXCLAMATION "Close the pyPTA launcher before installing or updating."
    Abort
  ${EndIf}
FunctionEnd

Section "pyPTA and bundled runtime (required)" Main
  SectionIn RO
  SetOutPath "$INSTDIR"
  ; Known payload names only; user data is outside the installation directory.
  Delete "$INSTDIR\AdaptiveCryptoDashboard.exe"
  Delete "$INSTDIR\AdaptiveCryptoDashboard-Source-1.0.0.zip"
  Delete "$INSTDIR\pyPTA-Source-1.0.1.zip"
  File /r "..\dist\pyPTA-${FLAVOR}\*"
!ifdef OPEN_SOURCE
  File "..\release\pyPTA-Source-1.0.1.zip"
!endif
  File /oname=README.txt "README-${FLAVOR}.txt"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${KEY}" "DisplayName" "pyPTA (${FLAVOR})"
  WriteRegStr HKCU "${KEY}" "DisplayVersion" "1.0.1"
  WriteRegStr HKCU "${KEY}" "Publisher" "EternuLL Organisation"
  WriteRegStr HKCU "${KEY}" "DisplayIcon" "$INSTDIR\pyPTA.exe"
  WriteRegStr HKCU "${KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${KEY}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "${KEY}" "QuietUninstallString" '$\"$INSTDIR\Uninstall.exe$\" /S'
  WriteRegDWORD HKCU "${KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "${KEY}" "EstimatedSize" $0
  Delete "$DESKTOP\Adaptive Crypto Dashboard.lnk"
  Delete "$SMPROGRAMS\Adaptive Crypto Dashboard\Adaptive Crypto Dashboard.lnk"
  Delete "$SMPROGRAMS\Adaptive Crypto Dashboard\Read me.lnk"
  Delete "$SMPROGRAMS\Adaptive Crypto Dashboard\Uninstall.lnk"
  RMDir "$SMPROGRAMS\Adaptive Crypto Dashboard"
  CreateDirectory "$SMPROGRAMS\${NAME}"
  CreateShortcut "$SMPROGRAMS\${NAME}\${NAME}.lnk" "$INSTDIR\pyPTA.exe"
  CreateShortcut "$SMPROGRAMS\${NAME}\Read me.lnk" "$INSTDIR\README.txt"
  CreateShortcut "$SMPROGRAMS\${NAME}\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd
Section "Desktop shortcut" Desktop
  CreateShortcut "$DESKTOP\${NAME}.lnk" "$INSTDIR\pyPTA.exe"
SectionEnd
Function un.onInit
  SetShellVarContext current
  SetRegView 64
  System::Call 'kernel32::OpenMutexW(i 0x100000, i 0, w "Local\AdaptiveCryptoDashboard.Installed") p.r0'
  ${If} $0 <> 0
    System::Call 'kernel32::CloseHandle(p r0)'
    MessageBox MB_ICONEXCLAMATION "Close the pyPTA launcher before uninstalling."
    Abort
  ${EndIf}
FunctionEnd
Section "Uninstall"
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\pyPTA.exe"
  Delete "$INSTDIR\pyPTA-Source-1.0.1.zip"
  Delete "$INSTDIR\README.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Delete "$DESKTOP\${NAME}.lnk"
  Delete "$SMPROGRAMS\${NAME}\${NAME}.lnk"
  Delete "$SMPROGRAMS\${NAME}\Read me.lnk"
  Delete "$SMPROGRAMS\${NAME}\Uninstall.lnk"
  RMDir "$SMPROGRAMS\${NAME}"
  DeleteRegKey HKCU "${KEY}"
SectionEnd
