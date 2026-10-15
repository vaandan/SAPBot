<#
.SYNOPSIS
  SAPBot laptop installer (pilot version of SAPBot-Setup.exe). See DEPLOYMENT.md, section 5.

.DESCRIPTION
  Three modes:

  -Mode Self      (default, NO admin rights needed)
      Code, Python environment and data in one user-writable folder (default C:\SAPBot, falls back to
      %LOCALAPPDATA%\SAPBot if C:\ is locked). Optional Hub enrolment with -HubUrl/-PubKey/-Fingerprint.
      Governance is trust-based on this laptop until it is enrolled with the Hub.

  -Mode Machine   (IT, run elevated once per laptop - e.g. via Intune/SCCM)
      Code + Python environment in -InstallDir (default C:\Program Files\SAPBot, read-only for users),
      locked machine policy in %ProgramData%\SAPBot (Hub URL, Hub public key, corporate CA, require_hub),
      embedding model pre-fetched, All-Users Start-menu shortcuts. No local admin role can exist afterwards.

  -Mode User      (each consultant, normal rights, after -Mode Machine)
      Creates the per-user data folder (%LOCALAPPDATA%\SAPBot), enrols with the Hub (asks for the token)
      and registers SAPBot in Claude Desktop. Same as the Start-menu entry "SAPBot First-time Setup".

.EXAMPLE
  # pilot laptop, no admin:
  powershell -ExecutionPolicy Bypass -File C:\SAPBot\app\install\install.ps1
  # IT (elevated):
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Mode Machine -InstallDir "C:\Program Files\SAPBot" `
     -HubUrl https://sapbot-hub.corp.local:8443 -PubKey .\policy_signing.pub.pem -Fingerprint "AB12:...." -CaBundle .\corp_root_ca.pem
#>
param(
  [ValidateSet("Self", "Machine", "User")][string]$Mode = "Self",
  [string]$SapbotHome = "",                       # Self mode: code+data folder
  [string]$InstallDir = "C:\Program Files\SAPBot", # Machine/User mode: code + .venv
  [string]$HubUrl = "",
  [string]$PubKey = "",
  [string]$Fingerprint = "",
  [string]$CaBundle = "",
  [switch]$ImportSapGate,
  [switch]$NoMcp
)
$ErrorActionPreference = "Stop"
function Step($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE) - see the output above" } }
function IsAdmin { ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }

function Find-Python([string]$Scope) {
  $candidates = @(
    @{ Exe = "py"; Args = @("-3") }, @{ Exe = "py"; Args = @("-3.12") }, @{ Exe = "py"; Args = @("-3.11") },
    @{ Exe = "python"; Args = @() }
  )
  foreach ($c in $candidates) {
    try {
      $ok = & $c.Exe @($c.Args + @("-c", "import sys;print(sys.version_info[:2]>=(3,11))")) 2>$null
      if ($ok -eq "True") { return $c }
    } catch {}
  }
  Write-Host "Python 3.11+ not found. Installing Python 3.12 ($Scope scope) with winget..." -ForegroundColor Yellow
  winget install -e --id Python.Python.3.12 --scope $Scope --accept-package-agreements --accept-source-agreements
  Check "winget install Python (if winget is blocked, install Python 3.12 from your software portal)"
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
  return @{ Exe = "py"; Args = @("-3.12") }
}

function New-Venv([string]$Dir, [string]$App, [string]$Scope, [switch]$Editable) {
  $py = Find-Python $Scope
  Write-Host ("using: " + $py.Exe + " " + ($py.Args -join " "))
  $venvPy = Join-Path $Dir ".venv\Scripts\python.exe"
  if (-not (Test-Path $venvPy)) { & $py.Exe @($py.Args + @("-m", "venv", (Join-Path $Dir ".venv"))); Check "create venv" }
  & $venvPy -m pip install --upgrade pip --quiet; Check "pip upgrade"
  $target = if ($Editable) { @("-e", $App) } else { @($App) }
  $wheels = Join-Path $App "wheelhouse"
  if (Test-Path $wheels) { & $venvPy -m pip install --no-index --find-links $wheels @target --quiet }   # offline
  else { & $venvPy -m pip install @target --quiet }
  Check "pip install SAPBot (behind a proxy: set HTTPS_PROXY; TLS inspection: set PIP_CERT=<corp CA>)"
  return $venvPy
}

function Add-ToPath([string]$Dir, [string]$Scope) {
  $cur = [Environment]::GetEnvironmentVariable("Path", $Scope)
  if ($cur -notlike "*$Dir*") { [Environment]::SetEnvironmentVariable("Path", "$cur;$Dir", $Scope) }
  $env:Path += ";$Dir"     # the 'sapbot' command works in new PowerShell windows
}

function New-Shortcuts([string]$Folder, [string]$VenvPy, [string]$WorkDir, [switch]$WithSetup) {
  New-Item -ItemType Directory -Force -Path $Folder | Out-Null
  $ws = New-Object -ComObject WScript.Shell
  $l = $ws.CreateShortcut((Join-Path $Folder "SAPBot Cockpit.lnk"))
  $l.TargetPath = $VenvPy; $l.Arguments = "-m sapbot.cli cockpit"; $l.WorkingDirectory = $WorkDir
  $l.Description = "SAPBot Cockpit (opens http://localhost:8501)"; $l.Save()
  $l = $ws.CreateShortcut((Join-Path $Folder "SAPBot Probe.lnk"))
  $l.TargetPath = "cmd.exe"; $l.Arguments = "/k `"$VenvPy`" -m sapbot.cli probe"; $l.WorkingDirectory = $WorkDir; $l.Save()
  $l = $ws.CreateShortcut((Join-Path $Folder "SAPBot Hub Status.lnk"))
  $l.TargetPath = "cmd.exe"; $l.Arguments = "/k `"$VenvPy`" -m sapbot.cli hub-status"; $l.WorkingDirectory = $WorkDir; $l.Save()
  if ($WithSetup) {
    $l = $ws.CreateShortcut((Join-Path $Folder "SAPBot First-time Setup.lnk"))
    $l.TargetPath = "cmd.exe"; $l.Arguments = "/k `"$VenvPy`" -m sapbot.cli user-setup"; $l.WorkingDirectory = $WorkDir; $l.Save()
  }
}

# =====================================================================================================  MACHINE
if ($Mode -eq "Machine") {
  if (-not (IsAdmin)) { throw "-Mode Machine must run elevated (IT). Consultants use -Mode User or -Mode Self." }
  $App = Join-Path $InstallDir "app"
  if (-not (Test-Path (Join-Path $App "pyproject.toml"))) { throw "Copy the SAPBot package to $App first" }
  if (-not $HubUrl -or -not $PubKey) { throw "-HubUrl and -PubKey are required for an IT-managed install" }

  Step "Python environment in $InstallDir (read-only for users)"
  $venvPy = New-Venv $InstallDir $App "machine"
  Add-ToPath (Join-Path $InstallDir ".venv\Scripts") "Machine"

  Step "Verify Hub public key"
  $fp = & $venvPy -c "import sys;from sapbot.security.signing import fingerprint;print(fingerprint(open(sys.argv[1],'rb').read()))" $PubKey
  Check "read public key"
  Write-Host "fingerprint: $fp"
  if ($Fingerprint -and ($Fingerprint.Replace(":", "").ToUpper() -ne $fp.Replace(":", ""))) {
    throw "Public key fingerprint mismatch - STOP and get the correct file from the Hub admin"
  }

  Step "Machine policy in $env:ProgramData\SAPBot (locked)"
  $md = Join-Path $env:ProgramData "SAPBot"
  New-Item -ItemType Directory -Force -Path $md | Out-Null
  Copy-Item $PubKey (Join-Path $md "hub_public.pem") -Force
  $caLine = ""
  if ($CaBundle) { Copy-Item $CaBundle (Join-Path $md "corporate_ca.pem") -Force; $caLine = "  ca_bundle: '$md\corporate_ca.pem'" }
  @"
# SAPBot machine policy - written by IT, read-only for users. Overrides every user setting.
require_hub: true
hub_bind_os_user: true
hub:
  url: '$HubUrl'
  public_key_file: '$md\hub_public.pem'
network:
$(if ($caLine) { $caLine } else { "  ca_bundle: ''" })
"@ | Set-Content -Encoding UTF8 (Join-Path $md "machine.yaml")
  icacls $md /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-545:(OI)(CI)RX" | Out-Null
  Check "set ACL on $md"

  Step "Pre-fetch embedding model into $md\models"
  if ($CaBundle) { $env:SSL_CERT_FILE = (Join-Path $md "corporate_ca.pem") }
  $env:SAPBOT_HOME = Join-Path $env:TEMP "sapbot-prefetch"
  & $venvPy -c "import sys;from pathlib import Path;from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2 as E;E.DOWNLOAD_PATH=Path(sys.argv[1]);E()(['x']);print('model ok')" (Join-Path $md "models\onnx")
  if ($LASTEXITCODE -ne 0) { Write-Host "Model download failed - SAPBot will use its offline fallback search until it is fetched." -ForegroundColor Yellow }

  Step "All-Users Start menu"
  New-Shortcuts (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\SAPBot") $venvPy $InstallDir -WithSetup
  Write-Host "`nMachine install done. Each consultant now opens: Start > SAPBot > SAPBot First-time Setup" -ForegroundColor Green
  exit 0
}

# =====================================================================================================  USER
if ($Mode -eq "User") {
  $venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
  if (-not (Test-Path $venvPy)) { throw "SAPBot is not installed in $InstallDir - ask IT (Mode Machine) or use -Mode Self" }
  & $venvPy -m sapbot.cli user-setup; Check "user setup"
  exit 0
}

# =====================================================================================================  SELF
if (-not $SapbotHome) {
  $SapbotHome = "C:\SAPBot"
  try { New-Item -ItemType Directory -Force -Path $SapbotHome -ErrorAction Stop | Out-Null }
  catch { $SapbotHome = Join-Path $env:LOCALAPPDATA "SAPBot"; Write-Host "C:\ is locked - using $SapbotHome" -ForegroundColor Yellow }
}
$App = Join-Path $SapbotHome "app"
if (-not (Test-Path (Join-Path $App "pyproject.toml"))) { throw "Code not found in $App (expected $App\pyproject.toml)" }

Step "Private Python environment in $SapbotHome\.venv (user scope, no admin)"
$venvPy = New-Venv $SapbotHome $App "user" -Editable
Add-ToPath (Join-Path $SapbotHome ".venv\Scripts") "User"

$env:SAPBOT_HOME = $SapbotHome
[Environment]::SetEnvironmentVariable("SAPBOT_HOME", $SapbotHome, "User")
if ($CaBundle) {
  New-Item -ItemType Directory -Force -Path (Join-Path $SapbotHome "config") | Out-Null
  Copy-Item $CaBundle (Join-Path $SapbotHome "config\corporate_ca.pem") -Force
  $env:SSL_CERT_FILE = (Join-Path $SapbotHome "config\corporate_ca.pem")
}

Step "Initialise SAPBot"
& $venvPy -m sapbot.cli init; Check "sapbot init"

Step "Pre-fetch the RAG embedding model (all-MiniLM-L6-v2, ONNX, ~90 MB, one time)"
& $venvPy -c "from sapbot.memory.embeddings import get_embedder; print('embedder:', get_embedder().name)"

if ($ImportSapGate -and (Test-Path "C:\Claude_CoWork\workspace\scripts")) {
  Step "Import sap-gate scripts (copy only)"
  & $venvPy -m sapbot.cli import-sapgate --src "C:\Claude_CoWork"; Check "import sap-gate scripts"
}

if ($HubUrl -and $PubKey) {
  Step "Enrol with the Hub (you will be asked for your token)"
  $a = @("-m", "sapbot.cli", "enroll", "--hub", $HubUrl, "--pubkey", $PubKey)
  if ($Fingerprint) { $a += @("--fingerprint", $Fingerprint) }
  if ($CaBundle) { $a += @("--ca-bundle", $CaBundle) }
  & $venvPy @a; Check "Hub enrolment"
}

if (-not $NoMcp) {
  Step "Register SAPBot tool server in Claude Desktop / Cowork"
  & $venvPy -m sapbot.cli mcp-register; Check "register MCP server"
}

Step "Start-menu shortcuts"
New-Shortcuts (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\SAPBot") $venvPy $SapbotHome

Write-Host "`nSAPBot installed in $SapbotHome." -ForegroundColor Green
Write-Host @"
Next steps:
  1. Log on to your QA system in SAP Logon (SAPBot never logs on for you).
  2. Laptop mode only (not enrolled): create the client workspace, e.g.
       & "$venvPy" -m sapbot.cli workspace create acme --name "ACME" --system SUQ/400/QA/write --system SUP/300/PROD
       & "$venvPy" -m sapbot.cli workspace use acme
  3. Start menu > SAPBot > SAPBot Probe
  4. Quit Claude Desktop from the tray icon and start it again -> 'sapbot' tools appear in Cowork.
"@
