; Inno Setup script for AutoShelf: a per-user install (no administrator rights) of the app
; that packaging\build.ps1 makes in dist\AutoShelf. Build it with packaging\installer.ps1.
;
; It adds a Start-menu shortcut that carries AutoShelf's AppUserModelID (the app's identity
; for the taskbar and notifications), registers the autoshelf: link that notification
; buttons open, and optionally adds the Explorer menu and the sign-in entry. Uninstalling
; removes all of that, including keys the app's Settings wrote (the same names as in
; autoshelf\explorer.py and autoshelf\startup.py; tests\test_installer.py checks that).
; AutoShelf's data in %APPDATA%\AutoShelf (history, settings) is kept, and your files are
; never touched.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define AppId "HarukiU06.AutoShelf"

[Setup]
AppId={{DBA4B6E6-9F40-435A-AEAC-04941C1B1D8F}
AppName=AutoShelf
AppVersion={#AppVersion}
AppVerName=AutoShelf {#AppVersion}
AppPublisher=HarukiU06
AppPublisherURL=https://github.com/HarukiU06/Folder-sorting-system
DefaultDirName={localappdata}\Programs\AutoShelf
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=AutoShelf-{#AppVersion}-setup
SetupIconFile=..\build\autoshelf.ico
UninstallDisplayIcon={app}\AutoShelf.exe
UninstallDisplayName=AutoShelf
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
ChangesAssociations=yes
LicenseFile=..\LICENSE

[Tasks]
Name: "explorermenu"; Description: "Add AutoShelf to Explorer's right-click menu (Sort, Why is this here?, Compress, Convert)"
Name: "startup"; Description: "Start AutoShelf when I sign in to Windows"; Flags: unchecked

[Files]
Source: "..\dist\AutoShelf\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\AutoShelf"; Filename: "{app}\AutoShelf.exe"; AppUserModelID: "{#AppId}"; Comment: "Sorts new files into your own folders"

[Registry]
; The autoshelf: link that notification buttons open, and the app's name for notifications.
Root: HKCU; Subkey: "Software\Classes\autoshelf"; ValueType: string; ValueName: ""; ValueData: "URL:AutoShelf"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\autoshelf"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\autoshelf\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\_internal\autoshelf.ico"
Root: HKCU; Subkey: "Software\Classes\autoshelf\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\AutoShelf.exe"" --url ""%1"""
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#AppId}"; ValueType: string; ValueName: "DisplayName"; ValueData: "AutoShelf"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#AppId}"; ValueType: string; ValueName: "IconUri"; ValueData: "{app}\_internal\autoshelf.ico"
; Start with Windows (a task here, or Settings later).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "AutoShelf"; ValueData: """{app}\AutoShelf.exe"" --background"; Tasks: startup; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "AutoShelf"; Flags: uninsdeletevalue dontcreatekey
; Explorer's menu: written by the app (see [Run]); removed here on uninstall.
Root: HKCU; Subkey: "Software\Classes\*\shell\AutoShelf"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.bmp\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.doc\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.docm\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.docx\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.heic\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.heif\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.jpeg\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.jpg\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.png\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.ppt\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pptx\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.rtf\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.tif\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.tiff\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.webp\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.xls\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.xlsm\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.xlsx\shell\AutoShelf.Convert"; Flags: uninsdeletekey dontcreatekey

[Run]
Filename: "{app}\autoshelf-cli.exe"; Parameters: "config --explorer-menu on"; Flags: runhidden; Tasks: explorermenu; StatusMsg: "Adding AutoShelf to Explorer's menu..."
Filename: "{app}\AutoShelf.exe"; Description: "Start AutoShelf"; Flags: nowait postinstall skipifsilent
; An update from the app (setup.exe /VERYSILENT ... /AUTOSHELFUPDATE) starts AutoShelf again.
Filename: "{app}\AutoShelf.exe"; Flags: nowait; Check: IsUpdate

[UninstallRun]
Filename: "{app}\AutoShelf.exe"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitAutoShelf"

[Code]
// True when the app started this setup to update itself (see autoshelf\updates.py).
function IsUpdate: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/AUTOSHELFUPDATE') = 0 then
      Result := True;
end;

// Quit a running AutoShelf before its files are replaced (an update).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  App: String;
  ResultCode: Integer;
begin
  App := ExpandConstant('{app}\AutoShelf.exe');
  if FileExists(App) then
  begin
    Exec(App, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(1500);
  end;
  Result := '';
end;
