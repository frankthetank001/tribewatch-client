; TribeWatch Inno Setup script
; Builds a Windows installer from PyInstaller output

#define MyAppName "TribeWatch"
#define MyAppVersion Trim(FileRead(FileOpen("VERSION")))
#define MyAppPublisher "TribeWatch"
#define MyAppURL "https://github.com/frankthetank001/tribewatch-client"
#define MyAppExeName "TribeWatch.exe"

[Setup]
AppId={{E4F3A1B2-7C5D-4E6F-8A9B-0C1D2E3F4A5B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=dist
OutputBaseFilename=TribeWatch-Setup
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
Name: "startupentry"; Description: "Start TribeWatch when Windows starts"; GroupDescription: "Startup:"

[Files]
Source: "dist\TribeWatch\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "tribewatch.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\tribewatch.ico"
Name: "{group}\{#MyAppName} Setup"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--setup"; IconFilename: "{app}\tribewatch.ico"
Name: "{group}\{#MyAppName} (Reset)"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--reset-all"; IconFilename: "{app}\tribewatch.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\tribewatch.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "TribeWatch"; ValueData: """{app}\{#MyAppExeName}"" --run"; Flags: uninsdeletevalue; Tasks: startupentry

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall
