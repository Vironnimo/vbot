#ifndef PayloadDir
  #error PayloadDir must name the staged package root
#endif
#ifndef BuildOutputDir
  #define BuildOutputDir "."
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef InstallShape
  #define InstallShape "server"
#endif
#ifndef VersionId
  #error VersionId must name the staged version directory
#endif
#ifndef ReleasePublicKey
  #define ReleasePublicKey ""
#endif
#ifndef UpdateChannel
  #define UpdateChannel "release"
#endif

[Setup]
AppId={{2F5A5391-1314-4E26-A37D-88A18049F235}
AppName=vBot
AppVersion={#AppVersion}
AppPublisher=The vBot Authors
DefaultDirName={localappdata}\Programs\vBot
DefaultGroupName=vBot
PrivilegesRequired=lowest
OutputDir={#BuildOutputDir}
OutputBaseFilename=vBot-{#AppVersion}-windows-x86_64-{#InstallShape}
Compression=lzma2/max
SolidCompression=yes
SetupIconFile={#SourcePath}\..\..\desktop\icon.ico
UninstallDisplayIcon={app}\vBot.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes

[Files]
Source: "{#PayloadDir}\vBot.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\vBot.GUI.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\versions\{#VersionId}\*"; DestDir: "{tmp}\vbot-payload"; Flags: recursesubdirs createallsubdirs deleteafterinstall

[Tasks]
#if InstallShape != "desktop-client"
Name: "startup"; Description: "Start vBot when I sign in"
#endif

[Icons]
Name: "{group}\vBot"; Filename: "{app}\vBot.exe"
#if InstallShape != "server"
Name: "{group}\vBot Desktop"; Filename: "{app}\vBot.GUI.exe"; Parameters: "desktop"
#endif

[UninstallDelete]
; Installation accepts only an empty folder and keeps server data outside it,
; so everything the application wrote there since belongs to it.
Type: filesandordirs; Name: "{app}"

[Code]
var
  RemovalReserved: Boolean;

function WithoutOwnedPath(CurrentValue: String; OwnedPath: String): String;
var
  Separator: Integer;
  Entry: String;
begin
  Result := '';
  while CurrentValue <> '' do
  begin
    Separator := Pos(';', CurrentValue);
    if Separator = 0 then
    begin
      Entry := CurrentValue;
      CurrentValue := '';
    end
    else
    begin
      Entry := Copy(CurrentValue, 1, Separator - 1);
      Delete(CurrentValue, 1, Separator);
    end;
    Entry := Trim(Entry);
    if (Entry <> '') and (CompareText(RemoveQuotes(Entry), OwnedPath) <> 0) then
    begin
      if Result <> '' then
        Result := Result + ';';
      Result := Result + Entry;
    end;
  end;
end;

function AddApplicationPath(): Boolean;
var
  CurrentValue: String;
  OwnedPath: String;
begin
  OwnedPath := ExpandConstant('{app}');
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', CurrentValue) then
    CurrentValue := '';
  CurrentValue := WithoutOwnedPath(CurrentValue, OwnedPath);
  if CurrentValue = '' then
    CurrentValue := OwnedPath
  else
    CurrentValue := OwnedPath + ';' + CurrentValue;
  Result := RegWriteExpandStringValue(HKCU, 'Environment', 'Path', CurrentValue);
end;

function RemoveApplicationPath(): Boolean;
var
  CurrentValue: String;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', CurrentValue) then
  begin
    Result := True;
    exit;
  end;
  Result := RegWriteExpandStringValue(
    HKCU, 'Environment', 'Path',
    WithoutOwnedPath(CurrentValue, ExpandConstant('{app}'))
  );
end;

function UnsafeParameter(const Value: String): Boolean;
begin
  Result := (Pos('"', Value) > 0) or (Pos(#13, Value) > 0) or (Pos(#10, Value) > 0);
end;

{ ' --name "value"', or nothing for an empty value. }
function OptionalOption(const Name, Value: String): String;
begin
  Result := '';
  if Value <> '' then
    Result := ' ' + Name + ' "' + Value + '"';
end;

{ One option per comma-separated value: /VBOTTRUSTEDPROXY=a,b passes two proxies. }
function RepeatedOption(const Name, Values: String): String;
var
  Rest: String;
  Item: String;
  Separator: Integer;
begin
  Result := '';
  Rest := Values;
  while Rest <> '' do
  begin
    Separator := Pos(',', Rest);
    if Separator = 0 then
    begin
      Item := Rest;
      Rest := '';
    end
    else
    begin
      Item := Copy(Rest, 1, Separator - 1);
      Rest := Copy(Rest, Separator + 1, Length(Rest));
    end;
    Result := Result + OptionalOption(Name, Trim(Item));
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExitCode: Integer;
  Parameters: String;
  ServerHost: String;
  ServerPort: String;
  ServerData: String;
  ServerAccess: String;
  AllowNetworks: String;
  AllowHosts: String;
  PublicUrl: String;
  TrustedProxies: String;
  TlsCert: String;
  TlsKey: String;
begin
  if CurStep <> ssPostInstall then
    exit;
#if InstallShape == "desktop-client"
  Parameters := 'application install --root "' + ExpandConstant('{app}') +
    '" --payload "' + ExpandConstant('{tmp}\vbot-payload') +
    '" --shape {#InstallShape} --public-key "{#ReleasePublicKey}" --channel {#UpdateChannel}';
#else
  { An empty host or access option leaves the default to the application installer:
    the access mode local, and the bind address of the chosen mode. }
  ServerHost := ExpandConstant('{param:VBOTHOST|}');
  ServerPort := ExpandConstant('{param:VBOTPORT|22332}');
  ServerData := ExpandConstant('{param:VBOTDATA|{%USERPROFILE}\.vbot}');
  ServerAccess := ExpandConstant('{param:VBOTACCESS|}');
  AllowNetworks := ExpandConstant('{param:VBOTALLOWNETWORK|}');
  AllowHosts := ExpandConstant('{param:VBOTALLOWHOST|}');
  PublicUrl := ExpandConstant('{param:VBOTPUBLICURL|}');
  TrustedProxies := ExpandConstant('{param:VBOTTRUSTEDPROXY|}');
  TlsCert := ExpandConstant('{param:VBOTTLSCERT|}');
  TlsKey := ExpandConstant('{param:VBOTTLSKEY|}');
  if UnsafeParameter(ServerHost) or UnsafeParameter(ServerPort) or
    UnsafeParameter(ServerData) or UnsafeParameter(ServerAccess) or
    UnsafeParameter(AllowNetworks) or UnsafeParameter(AllowHosts) or
    UnsafeParameter(PublicUrl) or UnsafeParameter(TrustedProxies) or
    UnsafeParameter(TlsCert) or UnsafeParameter(TlsKey) then
    RaiseException('vBot installer parameters contain unsupported characters.');
  Parameters := 'application install --root "' + ExpandConstant('{app}') +
    '" --payload "' + ExpandConstant('{tmp}\vbot-payload') +
    '" --shape {#InstallShape} --port "' + ServerPort +
    '" --data-dir "' + ServerData + '" --public-key "{#ReleasePublicKey}" --channel {#UpdateChannel}' +
    OptionalOption('--host', ServerHost) + OptionalOption('--access', ServerAccess) +
    RepeatedOption('--allow-network', AllowNetworks) + RepeatedOption('--allow-host', AllowHosts) +
    OptionalOption('--public-url', PublicUrl) +
    RepeatedOption('--trusted-proxy', TrustedProxies) + OptionalOption('--tls-cert', TlsCert) +
    OptionalOption('--tls-key', TlsKey);
#endif
  if not ExecAndLogOutput(ExpandConstant('{app}\vBot.exe'), Parameters, '', SW_HIDE,
    ewWaitUntilTerminated, ExitCode, nil) or (ExitCode <> 0) then
    RaiseException('vBot application configuration failed; setup cannot continue.');
  if not AddApplicationPath() then
    RaiseException('vBot could not register its per-user command path; setup cannot continue.');
#if InstallShape != "desktop-client"
  if WizardIsTaskSelected('startup') then
    if not ExecAndLogOutput(ExpandConstant('{app}\vBot.exe'), 'autostart enable', '', SW_HIDE,
      ewWaitUntilTerminated, ExitCode, nil) or (ExitCode <> 0) then
      RaiseException('vBot Autostart registration failed; setup cannot continue.');
#endif
end;

function DestinationHasEntries(): Boolean;
var
  FindRec: TFindRec;
begin
  Result := False;
  if FindFirst(AddBackslash(ExpandConstant('{app}')) + '*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          Result := True;
          exit;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\application.json')) then
    Result := 'vBot is already installed here. Use vbot update to update the existing application.'
  else if DestinationHasEntries() then
    Result := 'The selected vBot installation directory is not empty. Choose an empty directory.';
end;

function InitializeUninstall(): Boolean;
var
  ExitCode: Integer;
begin
  Result := ExecAndLogOutput(ExpandConstant('{app}\vBot.exe'), 'application exit', '', SW_HIDE,
    ewWaitUntilTerminated, ExitCode, nil) and (ExitCode = 0);
  if not Result then
  begin
    SuppressibleMsgBox('The vBot tray could not exit safely. Uninstall was cancelled.', mbError, MB_OK, IDOK);
    exit;
  end;
#if InstallShape == "desktop-client"
  Result := Result;
#else
  Result := ExecAndLogOutput(ExpandConstant('{app}\vBot.exe'), 'server stop', '', SW_HIDE,
    ewWaitUntilTerminated, ExitCode, nil) and (ExitCode = 0);
  if not Result then
    SuppressibleMsgBox('vBot could not stop safely. Uninstall was cancelled; your application and data remain in place.', mbError, MB_OK, IDOK);
#endif
  if Result then
  begin
    Result := ExecAndLogOutput(ExpandConstant('{app}\vBot.exe'), 'application removal-begin', '', SW_HIDE,
      ewWaitUntilTerminated, ExitCode, nil) and (ExitCode = 0);
    if not Result then
      SuppressibleMsgBox('vBot could not reserve application removal safely. Uninstall was cancelled.', mbError, MB_OK, IDOK)
    else
      RemovalReserved := True;
  end;
  if Result then
  begin
    Result := ExecAndLogOutput(ExpandConstant('{app}\vBot.exe'), 'autostart disable', '', SW_HIDE,
      ewWaitUntilTerminated, ExitCode, nil) and (ExitCode = 0);
    if not Result then
      SuppressibleMsgBox('vBot Autostart could not be removed safely. Uninstall was cancelled.', mbError, MB_OK, IDOK);
  end;
  if Result then
  begin
    Result := RemoveApplicationPath();
    if not Result then
      SuppressibleMsgBox('The vBot command path could not be removed safely. Uninstall was cancelled.', mbError, MB_OK, IDOK);
  end;
end;

procedure DeinitializeUninstall();
begin
  if RemovalReserved then
    DeleteFile(ExpandConstant('{app}\removal-pending.json'));
end;
