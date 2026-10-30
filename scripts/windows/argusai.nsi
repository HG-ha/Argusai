; ArgusAI NSIS installer.
; Encoding: UTF-8. build.py passes /INPUTCHARSET UTF8 (a BOM is optional).
; Built from python build.py --nsis. Required defines:
;   APP_NAME, APP_VERSION, APP_VERSION_QUAD, OUT_FILE, INSTALL_MODE
;   PAYLOAD_FILE (onefile) or DIST_DIR (folder)
; Optional: APP_ARCH (x86_64/arm64), ICON_FILE, PUBLISHER, WELCOME_BMP,
;           HEADER_BMP, NOTICE_ZH, NOTICE_EN

Unicode true
ManifestDPIAware true
CRCCheck on
SetCompressor /SOLID lzma
RequestExecutionLevel admin
ShowInstDetails show
ShowUnInstDetails show

!ifndef APP_NAME
  !define APP_NAME "ArgusAI"
!endif
!ifndef APP_VERSION
  !define APP_VERSION "0.0.0"
!endif
!ifndef APP_VERSION_QUAD
  !define APP_VERSION_QUAD "0.0.0.0"
!endif
!ifndef PUBLISHER
  !define PUBLISHER "ArgusAI"
!endif
!ifndef INSTALL_MODE
  !define INSTALL_MODE "folder"
!endif
!ifndef APP_ARCH
  !define APP_ARCH "x86_64"
!endif
!if "${APP_ARCH}" != "x86_64"
  !if "${APP_ARCH}" != "arm64"
    !error "APP_ARCH must be x86_64 or arm64"
  !endif
!endif
!ifndef OUT_FILE
  !error "OUT_FILE is required"
!endif

!include "MUI2.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!insertmacro GetSize
Var UpdatePID

; Match upstream x64.nsh on older NSIS releases without this helper.
; IsWow64Process2 returns the native IMAGE_FILE_MACHINE on Windows ARM64.
; Its NT shared-data fallback also covers older x64 Windows versions.
!ifndef GetNativeMachineArchitecture
  !macro ArgusAIGetNativeMachineArchitecture OUTVAR
    System::Call kernel32::GetCurrentProcess()p.s
    System::Call kernel32::IsWow64Process2(ps,*i,*i0s)
    Pop ${OUTVAR}
    ${If} ${OUTVAR} == 0
      System::Call '*0x7FFE002E(&i2.s)'
      Pop ${OUTVAR}
    ${EndIf}
  !macroend
  !define GetNativeMachineArchitecture "!insertmacro ArgusAIGetNativeMachineArchitecture "
!endif

Name "${APP_NAME} ${APP_VERSION}"
OutFile "${OUT_FILE}"
InstallDir "$PROGRAMFILES64\${APP_NAME}"
InstallDirRegKey HKLM "Software\${APP_NAME}" "InstallDir"
BrandingText "${APP_NAME} ${APP_VERSION}"

VIProductVersion "${APP_VERSION_QUAD}"
VIFileVersion "${APP_VERSION_QUAD}"
VIAddVersionKey /LANG=0 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=0 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=0 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=0 "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey /LANG=0 "CompanyName" "${PUBLISHER}"
VIAddVersionKey /LANG=0 "LegalCopyright" "Copyright (c) ${PUBLISHER}"

!ifdef ICON_FILE
  !define MUI_ICON "${ICON_FILE}"
  !define MUI_UNICON "${ICON_FILE}"
!endif
!ifdef WELCOME_BMP
  !define MUI_WELCOMEFINISHPAGE_BITMAP "${WELCOME_BMP}"
  !define MUI_UNWELCOMEFINISHPAGE_BITMAP "${WELCOME_BMP}"
!endif
!ifdef HEADER_BMP
  !define MUI_HEADERIMAGE
  !define MUI_HEADERIMAGE_BITMAP "${HEADER_BMP}"
  !define MUI_HEADERIMAGE_RIGHT
!endif
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_NOAUTOCLOSE
!define MUI_COMPONENTSPAGE_SMALLDESC
!define MUI_WELCOMEPAGE_TITLE_3LINES
!define MUI_FINISHPAGE_TITLE_3LINES
!define MUI_WELCOMEPAGE_TITLE "$(WELCOME_TITLE)"
!define MUI_WELCOMEPAGE_TEXT "$(WELCOME_TEXT)"
!define MUI_FINISHPAGE_TITLE "$(FINISH_TITLE)"
!define MUI_FINISHPAGE_TEXT "$(FINISH_TEXT)"
!ifndef LAUNCH_VBS
  !define LAUNCH_VBS "scripts\windows\launch-gui.vbs"
!endif
!ifndef NOTICE_ZH
  !define NOTICE_ZH "scripts\packaging\notice.zh.txt"
!endif
!ifndef NOTICE_EN
  !define NOTICE_EN "scripts\packaging\notice.en.txt"
!endif
; MUI's default finish-page action is Exec()/CreateProcess, which cannot
; start a .vbs. Call a function that ShellExecutes the launcher instead
; (same association as the Start Menu / desktop shortcuts).
!define MUI_FINISHPAGE_RUN "$INSTDIR\launch-gui.vbs"
!define MUI_FINISHPAGE_RUN_TEXT "$(FINISH_RUN)"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchArgusAI
!define MUI_LICENSEPAGE_BUTTON "$(LICENSE_NEXT)"
!define MUI_LICENSEPAGE_TEXT_TOP "$(LICENSE_TOP)"
!define MUI_LICENSEPAGE_TEXT_BOTTOM "$(LICENSE_BOTTOM)"
!define MUI_LANGDLL_REGISTRY_ROOT "HKCU"
!define MUI_LANGDLL_REGISTRY_KEY "Software\${APP_NAME}"
!define MUI_LANGDLL_REGISTRY_VALUENAME "InstallerLanguage"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "$(license)"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"
LicenseLangString license ${LANG_SIMPCHINESE} "${NOTICE_ZH}"
LicenseLangString license ${LANG_ENGLISH} "${NOTICE_EN}"

LangString WELCOME_TITLE ${LANG_SIMPCHINESE} "安装 ${APP_NAME} 安全工作台"
LangString WELCOME_TITLE ${LANG_ENGLISH} "Install the ${APP_NAME} Security Workbench"
LangString WELCOME_TEXT ${LANG_SIMPCHINESE} "${APP_NAME} 是本机安全工作台：AI 助手、被动代理、逆向工具、检查与报告。$\r$\n$\r$\n只用于你自己的站点、样本，或已获授权的目标。下一页是使用说明。"
LangString WELCOME_TEXT ${LANG_ENGLISH} "${APP_NAME} is a local security workbench: AI assistant, passive proxy, reverse tools, checks, and reports.$\r$\n$\r$\nUse it only on targets you own or are authorized to test. The next page is the software notice."
LangString LICENSE_TOP ${LANG_SIMPCHINESE} "${APP_NAME} 按 GNU GPL 第 3 版提供；第三方组件保留各自许可。"
LangString LICENSE_TOP ${LANG_ENGLISH} "${APP_NAME} is provided under GNU GPL version 3; third-party licenses remain applicable."
LangString LICENSE_BOTTOM ${LANG_SIMPCHINESE} "运行软件无需接受 GPL；修改或传播时须遵守适用条款。"
LangString LICENSE_BOTTOM ${LANG_ENGLISH} "Acceptance is not required to run the software. Applicable terms govern modification and distribution."
LangString LICENSE_NEXT ${LANG_SIMPCHINESE} "继续(&C)"
LangString LICENSE_NEXT ${LANG_ENGLISH} "Continue"
LangString FINISH_TITLE ${LANG_SIMPCHINESE} "${APP_NAME} 已安装完成"
LangString FINISH_TITLE ${LANG_ENGLISH} "${APP_NAME} has been installed"
LangString FINISH_TEXT ${LANG_SIMPCHINESE} "登录自启可在应用设置里打开，也会出现在「系统设置 → 应用 → 启动」。"
LangString FINISH_TEXT ${LANG_ENGLISH} "Turn on Start at logon in Settings. It also appears under Windows Settings → Apps → Startup."
LangString FINISH_RUN ${LANG_SIMPCHINESE} "启动 ${APP_NAME}"
LangString FINISH_RUN ${LANG_ENGLISH} "Launch ${APP_NAME}"
LangString SEC_APP ${LANG_SIMPCHINESE} "${APP_NAME}（必需）"
LangString SEC_APP ${LANG_ENGLISH} "${APP_NAME} (required)"
LangString SEC_DESKTOP ${LANG_SIMPCHINESE} "桌面快捷方式"
LangString SEC_DESKTOP ${LANG_ENGLISH} "Desktop shortcut"
LangString SEC_AUTOSTART ${LANG_SIMPCHINESE} "登录后托盘启动"
LangString SEC_AUTOSTART ${LANG_ENGLISH} "Start in tray at logon"
LangString DESC_APP ${LANG_SIMPCHINESE} "安装程序文件、开始菜单快捷方式和卸载项。"
LangString DESC_APP ${LANG_ENGLISH} "Installs the application, Start Menu shortcut, and uninstaller."
LangString DESC_DESKTOP ${LANG_SIMPCHINESE} "在桌面放一个快捷方式。"
LangString DESC_DESKTOP ${LANG_ENGLISH} "Add a shortcut on the desktop."
LangString DESC_AUTOSTART ${LANG_SIMPCHINESE} "写入当前用户的启动项，登录后在托盘打开。可在系统设置里关闭。"
LangString DESC_AUTOSTART ${LANG_ENGLISH} "Add a per-user Startup entry so ${APP_NAME} opens in the tray after sign-in."

; InstallDirRegKey is evaluated before .onInit, while this installer is a
; 32-bit process. Without SetRegView 64 it reads Wow6432Node and misses the
; native key written by a previous install (e.g. D:\ArgusAI).
Function RestoreExistingInstallDir
  Push $0
  StrCpy $0 ""
  SetRegView 64
  ReadRegStr $0 HKLM "Software\${APP_NAME}" "InstallDir"
  ${If} $0 == ""
    ReadRegStr $0 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation"
  ${EndIf}
  ${If} $0 == ""
    SetRegView 32
    ReadRegStr $0 HKLM "Software\${APP_NAME}" "InstallDir"
    ${If} $0 == ""
      ReadRegStr $0 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation"
    ${EndIf}
    SetRegView 64
  ${EndIf}
  ${If} $0 != ""
    StrCpy $INSTDIR $0
  ${EndIf}
  Pop $0
FunctionEnd

; NSIS uses a 32-bit installer stub, including for a native ARM64 payload.
; Older x64.nsh RunningX64 checks can return false on Windows ARM64, and
; GetNativeSystemInfo can report an emulated x86/x64 CPU. Use the native
; machine returned by IsWow64Process2 / the upstream NSIS helper instead.
!macro DefineArchitectureCheck PREFIX
Function ${PREFIX}RequireTargetArchitecture
  Push $0
  ${GetNativeMachineArchitecture} $0
  !if "${APP_ARCH}" == "arm64"
    ${If} $0 != 43620 ; IMAGE_FILE_MACHINE_ARM64
      MessageBox MB_ICONSTOP "This installer requires ARM64 Windows. Download the installer for your CPU architecture."
      Abort
    ${EndIf}
  !else
    ${If} $0 != 34404 ; IMAGE_FILE_MACHINE_AMD64
      MessageBox MB_ICONSTOP "This installer requires x64 Windows. Download the installer for your CPU architecture."
      Abort
    ${EndIf}
  !endif
  Pop $0
FunctionEnd
!macroend

!insertmacro DefineArchitectureCheck ""
!insertmacro DefineArchitectureCheck "un."

Function .onInit
  Call RequireTargetArchitecture
  SetRegView 64
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/UPDATEPID=" $UpdatePID
  ${If} ${Errors}
    StrCpy $UpdatePID ""
    Call RestoreExistingInstallDir
  ${Else}
    ; Accept only the canonical positive PID passed by our update helper.
    IntFmt $1 "%u" $UpdatePID
    ${If} $1 != $UpdatePID
    ${OrIf} $UpdatePID == 0
      Abort "Invalid update process ID"
    ${EndIf}
  ${EndIf}
  !insertmacro MUI_LANGDLL_DISPLAY
FunctionEnd

Function un.onInit
  Call un.RequireTargetArchitecture
  SetRegView 64
  !insertmacro MUI_UNGETLANGUAGE
FunctionEnd

Function LaunchArgusAI
  ; Drop the elevated installer token so the GUI matches a normal shortcut.
  ; explorer.exe is the logged-on shell; it opens the .vbs via wscript.
  Exec '"$WINDIR\explorer.exe" "$INSTDIR\launch-gui.vbs"'
FunctionEnd

!macro KillArgusAI
  nsExec::ExecToLog 'taskkill /F /IM "${APP_NAME}.exe"'
  Pop $0
!macroend

Function WaitUpdateParent
  ; Never kill other ArgusAI instances during an application update.
  System::Call 'kernel32::OpenProcess(i 0x00100000, i 0, i $UpdatePID) p.r1'
  ${If} $1 == 0
    System::Call 'kernel32::GetLastError() i.r2'
    ${If} $2 != 87 ; ERROR_INVALID_PARAMETER: parent has already exited
      SetErrorLevel 1
      Abort "Cannot verify that ArgusAI has exited"
    ${EndIf}
  ${Else}
    System::Call 'kernel32::WaitForSingleObject(p r1, i 120000) i.r2'
    System::Call 'kernel32::CloseHandle(p r1)'
    ${If} $2 != 0
      SetErrorLevel 1
      Abort "ArgusAI did not exit; update cancelled"
    ${EndIf}
  ${EndIf}
FunctionEnd

Section "$(SEC_APP)" SecApp
  SectionIn RO
  ${If} $UpdatePID != ""
    Call WaitUpdateParent
  ${Else}
    !insertmacro KillArgusAI
  ${EndIf}
  SetOutPath "$INSTDIR"

  !if "${INSTALL_MODE}" == "onefile"
    !ifndef PAYLOAD_FILE
      !error "PAYLOAD_FILE is required for onefile installs"
    !endif
    File "${PAYLOAD_FILE}"
    !ifndef LICENSES_DIR
      !error "LICENSES_DIR is required for onefile installs"
    !endif
    SetOutPath "$INSTDIR\licenses"
    File /r "${LICENSES_DIR}\*.*"
    SetOutPath "$INSTDIR"
  !else
    !ifndef DIST_DIR
      !error "DIST_DIR is required for folder installs"
    !endif
    File /r "${DIST_DIR}\*.*"
  !endif

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  File /oname=launch-gui.vbs "${LAUNCH_VBS}"
  WriteRegStr HKLM "Software\${APP_NAME}" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\${APP_NAME}" "InstallArchitecture" "${APP_ARCH}"

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$SYSDIR\wscript.exe" '"$INSTDIR\launch-gui.vbs"' "$INSTDIR\${APP_NAME}.exe" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\Uninstall.lnk" "$INSTDIR\Uninstall.exe"

  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayIcon" "$INSTDIR\${APP_NAME}.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "EstimatedSize" $0
SectionEnd

Section "$(SEC_DESKTOP)" SecDesktop
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$SYSDIR\wscript.exe" '"$INSTDIR\launch-gui.vbs"' "$INSTDIR\${APP_NAME}.exe" 0
SectionEnd

Section /o "$(SEC_AUTOSTART)" SecAutostart
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${APP_NAME}" '"$INSTDIR\${APP_NAME}.exe" --gui --tray'
  WriteRegBin HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" "${APP_NAME}" 020000000000000000000000
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecApp} $(DESC_APP)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} $(DESC_DESKTOP)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAutostart} $(DESC_AUTOSTART)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  !insertmacro KillArgusAI
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Uninstall.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${APP_NAME}"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" "${APP_NAME}"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
  DeleteRegKey HKLM "Software\${APP_NAME}"
  RMDir /r "$INSTDIR"
SectionEnd
