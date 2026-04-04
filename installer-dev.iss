; TribeWatch (Dev) Inno Setup script
; Installs alongside stable — separate AppId, folder, and start menu group

#define MyAppName "TribeWatch (Dev)"
#define MyAppVersion Trim(FileRead(FileOpen("VERSION")))
#define MyAppPublisher "TribeWatch"
#define MyAppURL "https://github.com/frankthetank001/tribewatch-client"
#define MyAppExeName "TribeWatch.exe"

[Setup]
AppId={{B7A2C3D4-9E1F-4A5B-8C6D-2E3F4A5B6C7D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\TribeWatch-Dev
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=dist
OutputBaseFilename=TribeWatch-Dev-Setup
SetupIconFile=tribewatch.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupentry"; Description: "Start TribeWatch (Dev) when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\TribeWatch\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "tribewatch.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\tribewatch.ico"
Name: "{group}\{#MyAppName} Setup"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--setup"; IconFilename: "{app}\tribewatch.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\tribewatch.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "TribeWatch-Dev"; ValueData: """{app}\{#MyAppExeName}"" --run"; Flags: uninsdeletevalue; Tasks: startupentry

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall
