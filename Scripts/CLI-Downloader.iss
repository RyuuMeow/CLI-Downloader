#define AppName "CLI-Downloader"

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\portable\bundle\CLI-Downloader"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef IconFile
  #define IconFile "..\assets\icon\CLI-Downloader.ico"
#endif

[Setup]
AppId={{A7C9C3D1-9C4A-4D3F-8D77-8A0D7E6F1C42}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=CLI-Downloader contributors
AppPublisherURL=https://github.com/RyuuMeow/CLI-Downloader
AppSupportURL=https://github.com/RyuuMeow/CLI-Downloader
AppUpdatesURL=https://github.com/RyuuMeow/CLI-Downloader/releases
DefaultDirName={localappdata}\Programs\CLI-Downloader
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=CLI-Downloader-{#AppVersion}-windows-x64-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\CLI-Downloader.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
Uninstallable=yes

[Tasks]
Name: "addtopath"; Description: "Add CLI-Downloader to the user PATH"; GroupDescription: "Command-line integration:"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\CLI-Downloader.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\CLI-Downloader.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Code]
const
  UserEnvironmentKey = 'Environment';
  BroadcastWindow = $FFFF;
  SettingChangedMessage = $001A;
  AbortIfHung = $0002;

function SendMessageTimeout(hWnd, Msg, wParam, lParam, fuFlags, uTimeout: Integer;
  var lpdwResult: Integer): Integer;
  external 'SendMessageTimeoutW@user32.dll stdcall';

function UserPathContains(const PathValue, Item: string): Boolean;
var
  Remaining, Token: string;
  Separator: Integer;
begin
  Result := False;
  Remaining := PathValue;
  while Remaining <> '' do begin
    Separator := Pos(';', Remaining);
    if Separator = 0 then begin
      Token := Remaining;
      Remaining := '';
    end else begin
      Token := Copy(Remaining, 1, Separator - 1);
      Delete(Remaining, 1, Separator);
    end;
    if CompareText(Trim(Token), Item) = 0 then begin
      Result := True;
      Exit;
    end;
  end;
end;

function RemoveUserPathItem(const PathValue, Item: string): string;
var
  Remaining, Token: string;
  Separator: Integer;
begin
  Result := '';
  Remaining := PathValue;
  while Remaining <> '' do begin
    Separator := Pos(';', Remaining);
    if Separator = 0 then begin
      Token := Remaining;
      Remaining := '';
    end else begin
      Token := Copy(Remaining, 1, Separator - 1);
      Delete(Remaining, 1, Separator);
    end;
    Token := Trim(Token);
    if (Token <> '') and (CompareText(Token, Item) <> 0) then begin
      if Result <> '' then
        Result := Result + ';';
      Result := Result + Token;
    end;
  end;
end;

procedure BroadcastEnvironmentChange;
var
  ResultCode: Integer;
begin
  SendMessageTimeout(BroadcastWindow, SettingChangedMessage, 0, 0, AbortIfHung, 5000, ResultCode);
end;

procedure UpdateUserPath(AddEntry: Boolean);
var
  ExistingPath, InstallPath, UpdatedPath: string;
begin
  if not RegQueryStringValue(HKCU, UserEnvironmentKey, 'Path', ExistingPath) then
    ExistingPath := '';
  InstallPath := ExpandConstant('{app}');

  if AddEntry then begin
    if UserPathContains(ExistingPath, InstallPath) then
      Exit;
    if ExistingPath = '' then
      UpdatedPath := InstallPath
    else
      UpdatedPath := ExistingPath + ';' + InstallPath;
  end else begin
    if not UserPathContains(ExistingPath, InstallPath) then
      Exit;
    UpdatedPath := RemoveUserPathItem(ExistingPath, InstallPath);
  end;

  RegWriteStringValue(HKCU, UserEnvironmentKey, 'Path', UpdatedPath);
  BroadcastEnvironmentChange;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    UpdateUserPath(WizardIsTaskSelected('addtopath'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    UpdateUserPath(False);
end;
