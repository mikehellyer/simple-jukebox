; Inno Setup script for Simple-Jukebox.
; Built with: iscc packaging\windows\installer.iss
; Expects dist\Simple-Jukebox\ (PyInstaller onedir output) to already
; exist, and the version passed in via /DAppVersion=x.y.z (the release
; workflow does this so the .iss file never has to be hand-edited per
; release).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Simple-Jukebox"
#define RepoRoot AddBackslash(SourcePath) + "..\.."

[Setup]
AppId={{1F067D07-0BF7-4FC2-8F68-263BC2B9E0A0}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Mike Hellyer
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\Simple-Jukebox.exe
OutputDir={#RepoRoot}\dist
OutputBaseFilename=Simple-Jukebox-{#AppVersion}-Windows-Setup
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile={#RepoRoot}\packaging\icons\icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#RepoRoot}\dist\Simple-Jukebox\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\Simple-Jukebox.exe"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Simple-Jukebox.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Simple-Jukebox.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
