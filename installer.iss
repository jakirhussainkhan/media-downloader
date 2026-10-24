; Media Downloader - Inno Setup installer
; Requires: build first with .\build-exe.ps1, then compile this file with Inno Setup 6.
; Output: dist\MediaDownloader-Setup.exe
#define MyAppName "Media Downloader"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Media Downloader"
#define MyAppExeName "MediaDownloader.exe"

[Setup]
AppId={{8F3A2B1C-7D4E-4A5F-9C1B-3E6D8F0A2B4C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\MediaDownloader
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=MediaDownloader-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
CloseApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Keep user data (cookies) on uninstall so re-install stays logged in.
; Uncomment the line below to wipe data too:
; Type: filesandordirs; Name: "{userappdata}\MediaDownloader"
