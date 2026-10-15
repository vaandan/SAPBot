<#
.SYNOPSIS
  SAPBot Hub on the office Windows Server (pilot version of SAPBot-Hub-Setup.exe). See DEPLOYMENT.md, section 3.

.DESCRIPTION
  * Python environment in -HubHome (default C:\SAPBotHub) with the SAPBot package (+ hub extras)
  * policy signing keys, database, first Admin + token (printed once)
  * Windows service "SAPBotHub" via NSSM (https://nssm.cc), HTTPS with a certificate from the internal CA
  * optional dedicated service account (recommended) instead of LocalSystem
  * Windows Firewall inbound rule for the Hub port, limited to -AllowedRemote (e.g. office + VPN ranges)
  * Claude API key for the central proxy stored in keys\anthropic_api_key.txt (ACL: Administrators, SYSTEM,
    service account) - never on laptops

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\SAPBotHub\app\install\install_hub.ps1 -Admin vandan `
     -Cert C:\certs\sapbot-hub.crt -Key C:\certs\sapbot-hub.key -AllowedRemote "10.10.0.0/16,10.200.0.0/16" `
     -ServiceAccount "CORP\svc_sapbot"
#>
param(
  [Parameter(Mandatory = $true)][string]$Admin,
  [string]$HubHome = "C:\SAPBotHub",
  [int]$Port = 8443,
  [string]$Cert = "",
  [string]$Key = "",
  [string]$CaBundle = "",                 # corporate root CA for OUTBOUND calls through a TLS-inspecting proxy
  [string]$HttpsProxy = "",               # e.g. http://proxy.corp.local:8080 for outbound api.anthropic.com
  [string]$AllowedRemote = "LocalSubnet", # firewall scope: "LocalSubnet" or comma list of CIDR ranges
  [string]$ServiceAccount = "",           # e.g. CORP\svc_sapbot (asked for its password); empty = LocalSystem
  [string]$Nssm = "nssm",
  [string]$HubName = "",                  # DNS name on the certificate, for the health check (e.g. sapbot-hub.corp.local)
  [switch]$InsecureLanTest
)
$ErrorActionPreference = "Stop"
function Step($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this script in an elevated PowerShell (server administrator)." }
if (-not $InsecureLanTest -and (-not $Cert -or -not $Key)) {
  throw "HTTPS is required: pass -Cert and -Key (internal CA certificate for the Hub host name), or -InsecureLanTest for an isolated first test."
}
foreach ($p in @($Cert, $Key, $CaBundle, $HubHome)) {
  if ($p -and $p.Contains(" ")) { throw "Path '$p' contains spaces - use paths without spaces (e.g. C:\certs\...)." }
}
$App = Join-Path $HubHome "app"
if (-not (Test-Path (Join-Path $App "pyproject.toml"))) { throw "Copy the SAPBot package to $App first" }

Step "Python 3.11+ (machine-wide)"
$py = $null
foreach ($c in @(@("py", "-3"), @("py", "-3.12"), @("python"))) {
  try { $ok = & $c[0] @($c[1..($c.Count)] | Where-Object { $_ }) -c "import sys;print(sys.version_info[:2]>=(3,11))" 2>$null
        if ($ok -eq "True") { $py = $c; break } } catch {}
}
if (-not $py) { throw "Install Python 3.12 for all users first (python.org installer, 'Install for all users')." }
$venvPy = Join-Path $HubHome ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { & $py[0] @($py[1..($py.Count)] | Where-Object { $_ }) -m venv "$HubHome\.venv"; Check "create venv" }
& $venvPy -m pip install --upgrade pip --quiet; Check "pip upgrade"
& $venvPy -m pip install "$App[hub]" --quiet; Check "pip install SAPBot hub"
[Environment]::SetEnvironmentVariable("SAPBOT_HUB_HOME", $HubHome, "Machine")
$env:SAPBOT_HUB_HOME = $HubHome
$scripts = Join-Path $HubHome ".venv\Scripts"
$mp = [Environment]::GetEnvironmentVariable("Path", "Machine")
if ($mp -notlike "*$scripts*") { [Environment]::SetEnvironmentVariable("Path", "$mp;$scripts", "Machine") }
$env:Path += ";$scripts"          # 'sapbot-hub' command available in new elevated shells

Step "Keys, database, first admin"
if (Test-Path "$HubHome\keys\policy_signing.pem") {
  Write-Host "Existing signing key kept (re-keying would force every laptop to re-enrol)." -ForegroundColor Yellow
  & $venvPy -m sapbot.hub.app fingerprint; Check "fingerprint"
} else {
  & $venvPy -m sapbot.hub.app init --admin $Admin; Check "hub init"
  Write-Host "`n>>> Save the admin token and the fingerprint above in your password vault NOW. <<<" -ForegroundColor Yellow
}
$keyAcl = @("*S-1-5-32-544:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F")
if ($ServiceAccount) { $keyAcl += "${ServiceAccount}:(OI)(CI)R" }
icacls "$HubHome\keys" /inheritance:r /grant:r @keyAcl | Out-Null; Check "ACL on keys"
if ($ServiceAccount) {
  icacls "$HubHome" /grant "${ServiceAccount}:(OI)(CI)M" | Out-Null
  foreach ($f in @($Cert, $Key, $CaBundle)) { if ($f) { icacls $f /grant "${ServiceAccount}:R" | Out-Null } }
}

Step "Claude API key for the central proxy (stored in $HubHome\keys, never on laptops)"
$sec = Read-Host "Claude API key (Enter = keep the existing key / skip)" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
if ($plain) { Set-Content -Path "$HubHome\keys\anthropic_api_key.txt" -Value $plain -NoNewline -Encoding ASCII }
elseif (-not (Test-Path "$HubHome\keys\anthropic_api_key.txt")) { Write-Host "No key - Claude API mode stays unavailable." -ForegroundColor Yellow }

Step "Windows service SAPBotHub (NSSM)"
if (-not (Get-Command $Nssm -ErrorAction SilentlyContinue)) {
  throw "NSSM not found. Download nssm.exe (https://nssm.cc), get it approved, put it on PATH or pass -Nssm <path>."
}
$params = "-m sapbot.hub.app serve --port $Port"
if ($InsecureLanTest) { $params += " --insecure-lan-test" } else { $params += " --cert $Cert --key $Key" }
if ($CaBundle) { $params += " --ca-bundle $CaBundle" }
if (Get-Service SAPBotHub -ErrorAction SilentlyContinue) {
  try { & $Nssm stop SAPBotHub | Out-Null } catch {}
  try { & $Nssm remove SAPBotHub confirm | Out-Null } catch {}
}
& $Nssm install SAPBotHub $venvPy; Check "nssm install"
& $Nssm set SAPBotHub AppParameters $params | Out-Null; Check "nssm parameters"
& $Nssm set SAPBotHub AppDirectory $HubHome | Out-Null
$envs = @("SAPBOT_HUB_HOME=$HubHome")
if ($HttpsProxy) { $envs += "HTTPS_PROXY=$HttpsProxy" }
& $Nssm set SAPBotHub AppEnvironmentExtra @envs | Out-Null
& $Nssm set SAPBotHub AppStdout "$HubHome\logs\hub.out.log" | Out-Null
& $Nssm set SAPBotHub AppStderr "$HubHome\logs\hub.err.log" | Out-Null
& $Nssm set SAPBotHub AppRotateFiles 1 | Out-Null
& $Nssm set SAPBotHub AppRotateBytes 10485760 | Out-Null
& $Nssm set SAPBotHub Start SERVICE_AUTO_START | Out-Null
if ($ServiceAccount) {
  $cred = Get-Credential -UserName $ServiceAccount -Message "Password of the SAPBot Hub service account"
  & $Nssm set SAPBotHub ObjectName $ServiceAccount $cred.GetNetworkCredential().Password | Out-Null
}
& $Nssm start SAPBotHub; Check "start service"

Step "Windows Firewall"
Remove-NetFirewallRule -DisplayName "SAPBot Hub $Port" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "SAPBot Hub $Port" -Direction Inbound -Protocol TCP -LocalPort $Port `
  -RemoteAddress ($AllowedRemote -split ",") -Action Allow -Profile Domain,Private | Out-Null

Step "Health check"
Start-Sleep -Seconds 4
$scheme = if ($InsecureLanTest) { "http" } else { "https" }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$name = if ($HubName) { $HubName } else { [System.Net.Dns]::GetHostEntry('').HostName }   # must match the certificate
try { Invoke-RestMethod "${scheme}://${name}:$Port/api/health" | Out-String | Write-Host }
catch { Write-Host "Health check failed: $_  - see $HubHome\logs\hub.err.log" -ForegroundColor Red }

Write-Host @"

Hub installed. Next (DEPLOYMENT.md section 4):
  * distribute $HubHome\keys\policy_signing.pub.pem to laptops via IT software distribution + publish the fingerprint
  * in a NEW elevated PowerShell:  sapbot-hub workspace create ... ; sapbot-hub user <windows-user> --workspace <id>
  * nightly backup of $HubHome\data, \workspaces, \scripts; keys\ once into the password vault / encrypted backup
"@ -ForegroundColor Green
