#define MyAppName "ManPass"
#define MyAppVersion "3.6.1"
#define MyAppExe "ManPass.exe"
[Setup]
AppId={{67B33114-7DA1-43DB-A776-E020E71A6B2F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=6IX7EVEN
DefaultDirName={localappdata}\Programs\ManPass
DefaultGroupName=ManPass
OutputDir=..\dist\installer
OutputBaseFilename=ManPass-Setup
SetupIconFile=..\assets\manpass.ico
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest
WizardStyle=modern
LicenseFile=..\docs\LICENSE_AGREEMENT_RU.txt
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\ManPass.exe
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
[Files]
Source: "..\dist\ManPass\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{autoprograms}\ManPass"; Filename: "{app}\ManPass.exe"
Name: "{autodesktop}\ManPass"; Filename: "{app}\ManPass.exe"; Tasks: desktopicon
[Tasks]
Name: desktopicon; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки"; Flags: unchecked
[Run]
Filename: "{app}\ManPass.exe"; Description: "Запустить ManPass"; Flags: nowait postinstall skipifsilent
; User data is intentionally NOT deleted by uninstall.
