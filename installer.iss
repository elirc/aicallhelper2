; Inno Setup 6 script wrapping the PyInstaller folder build.
; Build the app first:  .venv\Scripts\pyinstaller aica.spec
; Then compile this with the Inno Setup Compiler (iscc installer.iss).

[Setup]
AppName=AI Call Assistant
AppVersion=3.0.0
AppPublisher=AI Call Assistant
DefaultDirName={autopf}\AI Call Assistant
DefaultGroupName=AI Call Assistant
UninstallDisplayIcon={app}\AICallAssistant.exe
OutputBaseFilename=AICallAssistant-Setup-3.0.0
OutputDir=dist
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest

[Files]
Source: "dist\AICallAssistant\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\AI Call Assistant"; Filename: "{app}\AICallAssistant.exe"
Name: "{autodesktop}\AI Call Assistant"; Filename: "{app}\AICallAssistant.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\AICallAssistant.exe"; Description: "Launch AI Call Assistant"; Flags: nowait postinstall skipifsilent
