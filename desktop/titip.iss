; Titip for Windows - per-user installer (no admin rights). build.py compiles it with:
;   iscc /DAppVersion=1.0.0 /DSourceDir=<dist\Titip> /DOutputDir=<dist> /DIconFile=<build\titip.ico> titip.iss
; Installs to %LOCALAPPDATA%\Programs\Titip, adds Titip to the Start menu (and optionally the desktop) and lists it
; in Settings > Apps. The window keeps its data (what you saved in it) in %LOCALAPPDATA%\Titip, which
; uninstalling leaves in place so a reinstall finds it again.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "dist\Titip"
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifndef IconFile
  #define IconFile "build\titip.ico"
#endif

[Setup]
AppId={{E81247BA-698B-569E-A6DE-E1E1C366EEFB}
AppName=Titip
AppVersion={#AppVersion}
AppVerName=Titip {#AppVersion}
AppPublisher=Juwen Lie
AppPublisherURL=https://juw12.github.io/titip/
AppSupportURL=https://github.com/juw12/titip
DefaultDirName={autopf}\Titip
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=Titip-Windows-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\Titip.exe
UninstallDisplayName=Titip
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Titip"; Filename: "{app}\Titip.exe"; Comment: "Your Outlook mail on your phone, served by your own PC"
Name: "{userdesktop}\Titip"; Filename: "{app}\Titip.exe"; Comment: "Your Outlook mail on your phone, served by your own PC"; Tasks: desktopicon

[Run]
Filename: "{app}\Titip.exe"; Description: "{cm:LaunchProgram,Titip}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\Titip.exe"; Parameters: "--stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopTitip"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  { Updating: close an open Titip window first so its files can be replaced. }
  if FileExists(ExpandConstant('{app}\Titip.exe')) then
    Exec(ExpandConstant('{app}\Titip.exe'), '--stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
