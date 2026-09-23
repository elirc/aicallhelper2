; Inno Setup 6 script wrapping the PyInstaller folder build.
; Build the app first:  .venv\Scripts\pyinstaller aica.spec
; Then compile:         iscc /DAppVersion=<version> installer.iss
; build.ps1 and CI pass /DAppVersion from pyproject.toml. The fallback below must
; still equal pyproject's version: tools/release_meta.py check fails on drift.

#ifndef AppVersion
#define AppVersion "3.1.0"
#endif

[Setup]
; Explicit, and equal to Inno's default (AppName), so upgrades from installers that
; did not set it still replace the previous install. Never change it.
AppId=AI Call Assistant
AppName=AI Call Assistant
AppVersion={#AppVersion}
AppVerName=AI Call Assistant {#AppVersion}
AppPublisher=AI Call Assistant
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
DefaultDirName={autopf}\AI Call Assistant
DefaultGroupName=AI Call Assistant
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\AICallAssistant.exe
OutputBaseFilename=AICallAssistant-Setup-{#AppVersion}
OutputDir=dist
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Per-user install: no UAC prompt, no admin rights, nothing written outside the
; user's profile ({autopf} resolves to %LOCALAPPDATA%\Programs in this mode).
PrivilegesRequired=lowest
CloseApplications=yes

[Files]
Source: "dist\AICallAssistant\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\AI Call Assistant"; Filename: "{app}\AICallAssistant.exe"
Name: "{autodesktop}\AI Call Assistant"; Filename: "{app}\AICallAssistant.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\AICallAssistant.exe"; Description: "Launch AI Call Assistant"; Flags: nowait postinstall skipifsilent

; Uninstall removes only {app}. Settings, encrypted keys and logs in the user's
; data folder are deliberately kept (see fabledocs/SETUP-AND-DEPLOY.md).
