; Inno Setup script: wraps the PyInstaller folder into one installer, dist\MoMGenerator-Setup.exe.
; Built by installer\build.ps1 (which passes /DAppVersion=...). Installs per user: no admin rights needed.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "MoM Generator"
#define AppExe "MoMGenerator.exe"

[Setup]
AppId={{6F3B2C1E-8D4A-4E7B-9C2F-5A1D3E9B7C40}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=MoM Generator
DefaultDirName={autopf}\{#AppName}
DisableDirPage=auto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=MoMGenerator-Setup
SetupIconFile=..\mom.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; an older version that is running is closed first
AppMutex=MoMGenerator_SingleInstance
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start MoM Generator when Windows starts (needed to record Teams meetings automatically)"; GroupDescription: "Startup:"; Flags: unchecked

[InstallDelete]
; files of the previous version
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\build\dist\MoMGenerator\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopApp"

[UninstallDelete]
; the start-with-Windows entry the app itself can create
Type: files; Name: "{app}\*.log"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#AppName}');
end;
