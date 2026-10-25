; Inno Setup script for BeatSync - compiled by build\build.bat or the GitHub workflow.
; Per-user install (no admin rights needed) into %LOCALAPPDATA%\Programs\BeatSync.

#ifndef AppVersion
  #define AppVersion "3.4.0"
#endif
#define AppName "BeatSync"
#define AppExe "BeatSync.exe"
#ifndef AppURL
  #define AppURL "https://github.com"
#endif

[Setup]
AppId={{8C1B8A3E-5B7E-4F1B-9D3A-2B6F0E7C4A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=OFGD
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=BeatSync-Setup-{#AppVersion}
SetupIconFile=..\assets\beatsync.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
LicenseFile=..\LICENSE
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\BeatSync\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; old library files from a previous version
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start BeatSync"; Flags: nowait postinstall

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
procedure KillBeatSync();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillBeatSync();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    KillBeatSync();

  if CurUninstallStep = usPostUninstall then
    if MsgBox('Also delete downloaded tools, settings and temporary files?' + #13#10 +
              ExpandConstant('{localappdata}\BeatSync'), mbConfirmation, MB_YESNO) = IDYES then
      DelTree(ExpandConstant('{localappdata}\BeatSync'), True, True, True);
end;
