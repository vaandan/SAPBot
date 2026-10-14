<#
  SAPBot laptop installer (pilot version of SAPBot-Setup.exe, ADR D1).

  Layout after install:
    C:\SAPBot\app\        the code (this package)
    C:\SAPBot\.venv\      private Python environment
    C:\SAPBot\config\     sapbot.yaml
    C:\SAPBot\data\       local store (users, grants, approvals, audit), runs
    C:\SAPBot\scripts\    VBS library
    C:\SAPBot\workspaces\ one folder per client

  Run in PowerShell (normal user is fine):
    powershell -ExecutionPolicy Bypass -File C:\SAPBot\app\install\install.ps1
  Options:
    -ImportSapGate   copy the sap-gate scripts from C:\Claude_CoWork (read-only; sap-gate is not changed)
    -NoMcp           do not register SAPBot in Claude Desktop / Cowork
#>
param(
  [string]$SapbotHome = "C:\SAPBot",
  [switch]$ImportSapGate,
  [switch]$NoMcp
)
$ErrorActionPreference = "Stop"
function Step($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE) - see the output above" } }

$App = Join-Path $SapbotHome "app"
if (-not (Test-Path (Join-Path $App "pyproject.toml"))) { throw "Code not found in $App (expected $App\pyproject.toml)" }

Step "Python 3.11+"
$candidates = @(
  @{ Exe = "py"; Args = @("-3") }, @{ Exe = "py"; Args = @("-3.12") }, @{ Exe = "py"; Args = @("-3.11") },
  @{ Exe = "python"; Args = @() }
)
$py = $null
foreach ($c in $candidates) {
  try {
    $ok = & $c.Exe @($c.Args + @("-c", "import sys;print(sys.version_info[:2]>=(3,11))")) 2>$null
    if ($ok -eq "True") { $py = $c; break }
  } catch {}
}
if (-not $py) {
  Write-Host "Python 3.11+ not found. Installing Python 3.12 for this user with winget..." -ForegroundColor Yellow
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  Check "winget install Python"
  # refresh PATH for this session so the new 'py' launcher is found
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
  $py = @{ Exe = "py"; Args = @("-3.12") }
}
Write-Host ("using: " + $py.Exe + " " + ($py.Args -join " "))

Step "Private environment in $SapbotHome\.venv"
$venvPy = Join-Path $SapbotHome ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { & $py.Exe @($py.Args + @("-m", "venv", "$SapbotHome\.venv")); Check "create venv" }
& $venvPy -m pip install --upgrade pip --quiet; Check "pip upgrade"
$wheels = Join-Path $App "wheelhouse"
if (Test-Path $wheels) {
  & $venvPy -m pip install --no-index --find-links $wheels -e "$App" --quiet     # offline install
} else {
  & $venvPy -m pip install -e "$App" --quiet
}
Check "pip install SAPBot"

$env:SAPBOT_HOME = $SapbotHome
[Environment]::SetEnvironmentVariable("SAPBOT_HOME", $SapbotHome, "User")

Step "Initialise SAPBot (first user on this laptop becomes Admin in laptop mode)"
& $venvPy -m sapbot.cli init; Check "sapbot init"

Step "Pre-fetch the RAG embedding model (all-MiniLM-L6-v2, ONNX, ~90 MB, one time)"
& $venvPy -c "from sapbot.memory.embeddings import get_embedder; print('embedder:', get_embedder().name)"

if ($ImportSapGate -and (Test-Path "C:\Claude_CoWork\workspace\scripts")) {
  Step "Import sap-gate scripts (copy only)"
  & $venvPy -m sapbot.cli import-sapgate --src "C:\Claude_CoWork"; Check "import sap-gate scripts"
}

if (-not $NoMcp) {
  Step "Register SAPBot tool server in Claude Desktop / Cowork"
  & $venvPy -m sapbot.cli mcp-register; Check "register MCP server"
}

Step "Start-menu shortcuts"
$sm = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\SAPBot"
New-Item -ItemType Directory -Force -Path $sm | Out-Null
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $sm "SAPBot Cockpit.lnk"))
$lnk.TargetPath = $venvPy
$lnk.Arguments = "-m sapbot.cli cockpit"
$lnk.WorkingDirectory = $SapbotHome
$lnk.Description = "SAPBot Cockpit (opens in your browser at http://localhost:8501)"
$lnk.Save()
$lnk2 = $ws.CreateShortcut((Join-Path $sm "SAPBot Probe.lnk"))
$lnk2.TargetPath = "cmd.exe"
$lnk2.Arguments = "/k `"$venvPy`" -m sapbot.cli probe"
$lnk2.WorkingDirectory = $SapbotHome
$lnk2.Save()

Write-Host "`nSAPBot installed." -ForegroundColor Green
Write-Host @"
Next steps:
  1. Log on to your QA system (e.g. SUQ/400) in SAP Logon.
  2. Create the client workspace:
       $venvPy -m sapbot.cli workspace create acme --name "ACME" --system SUQ/400/QA/write --system SUP/300/PROD
       $venvPy -m sapbot.cli workspace use acme
  3. Check everything:   Start menu > SAPBot Probe
  4. Quit Claude Desktop from the tray icon and start it again -> the 'sapbot' tools appear in Cowork.
  5. Open the Cockpit:   Start menu > SAPBot Cockpit
"@
