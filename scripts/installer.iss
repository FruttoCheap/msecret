; Windows installer: per user, no admin. Like the Mac app it carries the core's wheel, its pins and
; uv, and installs the core with `uv tool install` (network, a few minutes; Python and the
; dependencies come from python.org builds and PyPI). Built by .github/workflows/ci.yml:
; iscc /DVersion=<x.y.z> scripts\installer.iss, with build\ holding uv.exe, the wheel,
; constraints.txt and mcp-secret.ico.

; where uv tool install puts the UI
#define Gui "{%USERPROFILE}\.local\bin\mcp-secret-gui.exe"

[Setup]
AppName=mcp-secret
AppVersion={#Version}
AppId=it.clait.mcp-secret
DefaultDirName={localappdata}\Programs\mcp-secret
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=..\dist
OutputBaseFilename=mcp-secret-setup-{#Version}
SetupIconFile=..\build\mcp-secret.ico
UninstallDisplayIcon={app}\mcp-secret.ico
WizardStyle=modern
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "it"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
it.Core=Installazione del core: qualche minuto, serve la rete…
en.Core=Installing the core: a few minutes, needs the network…
it.CoreFailed=Installazione del core non riuscita. Controlla la connessione e avvia di nuovo l'installer. Dettagli: %1
en.CoreFailed=The core did not install. Check the connection and run the installer again. Details: %1
it.Open=Apri mcp-secret
en.Open=Open mcp-secret

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"

[Files]
Source: "..\build\*"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\mcp-secret"; Filename: "{#Gui}"; IconFilename: "{app}\mcp-secret.ico"
Name: "{userdesktop}\mcp-secret"; Filename: "{#Gui}"; IconFilename: "{app}\mcp-secret.ico"; Tasks: desktopicon

[Run]
Filename: "{#Gui}"; Description: "{cm:Open}"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; the index and the token map stay in %LOCALAPPDATA%\mcp-secret: they are the user's data
Filename: "{app}\uv.exe"; Parameters: "tool uninstall mcp-secret"; Flags: runhidden; RunOnceId: "core"
; the downloaded packages, a few GB; uv fetches them again if needed
Filename: "{app}\uv.exe"; Parameters: "cache clean"; Flags: runhidden; RunOnceId: "cache"

[UninstallDelete]
Type: files; Name: "{app}\install.log"

[Code]
// The core, after the files are in place. uv's output goes to install.log next to it: package
// names and versions, no document data. --reinstall: an update replaces a core of another version.
procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Log: String;
begin
  if CurStep <> ssPostInstall then
    exit;
  WizardForm.StatusLabel.Caption := CustomMessage('Core');
  WizardForm.ProgressGauge.Style := npbstMarquee;
  Log := ExpandConstant('{app}\install.log');
  // relative paths from {app}: a user name with a space stays one argument. The UI goes where
  // the shortcuts point, whatever UV_TOOL_BIN_DIR says. uv's own Python: a system-wide one is
  // where icacls cannot open it to the box
  if not Exec(ExpandConstant('{cmd}'), '/c set "UV_TOOL_BIN_DIR=%USERPROFILE%\.local\bin" && set "UV_MANAGED_PYTHON=1" && uv.exe tool install --force --reinstall --python 3.12 '
              + '-c constraints.txt mcp_secret-{#Version}-py3-none-any.whl > install.log 2>&1',
              ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
    MsgBox(FmtMessage(CustomMessage('CoreFailed'), [Log]), mbError, MB_OK);
  WizardForm.ProgressGauge.Style := npbstNormal;
end;
