; SPDX-License-Identifier: AGPL-3.0-or-later
; The Windows installer, compiled by Inno Setup (preinstalled on the GitHub runners).
; binaries.yml passes the two defines:
;   iscc /DAppVersion=2026.8.1 /DSourceDir=..\..\build\pyinstaller\dist\reaper packaging\windows\reaper.iss
;
; Per-user by default (no admin prompt, installs under %LOCALAPPDATA%\Programs), which
; is also what keeps the data folder (%LOCALAPPDATA%\Reaper, chosen by the launcher)
; owned by the same account that runs the server. The AppId is permanent: winget and
; every future upgrade key on it, so it must never change.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\build\pyinstaller\dist\reaper"
#endif

[Setup]
AppId={{CE896BD9-B87B-499A-AFA4-642FB789958D}
AppName=Reaper
AppVersion={#AppVersion}
AppPublisher=Scythe Labs
AppPublisherURL=https://github.com/scythe-labs/reaper
AppSupportURL=https://github.com/scythe-labs/reaper/issues
DefaultDirName={autopf}\Reaper
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=Reaper-{#AppVersion}-windows-x64-setup
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
LicenseFile=..\..\LICENSE
UninstallDisplayIcon={app}\reaper.exe

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Reaper"; Filename: "{app}\reaper.exe"

[Run]
; The server opens the operator's browser itself once it is healthy (the launcher's
; REAPER_LAUNCH_BROWSER default for a frozen build), so starting the exe is enough.
Filename: "{app}\reaper.exe"; Description: "Start Reaper now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop a running server so the uninstall never leaves half an install behind.
Filename: "{cmd}"; Parameters: "/C taskkill /IM reaper.exe /F"; Flags: runhidden; RunOnceId: "StopReaper"
