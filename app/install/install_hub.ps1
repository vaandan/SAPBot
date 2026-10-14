<#
  SAPBot Hub on the office Windows server (pilot version of SAPBot-Hub-Setup.exe, ADR D2, B12).

  powershell -ExecutionPolicy Bypass -File install_hub.ps1 -Admin vandan [-Port 8443] [-Cert hub.crt -Key hub.key]

  * installs the code into C:\SAPBotHub\app (copy this package there first) with a private Python environment
  * creates the policy signing keys, the database and the first Admin (+ token)
  * registers a Windows service "SAPBotHub" with NSSM (https://nssm.cc - put nssm.exe on PATH) so it starts with
    the server; without NSSM it prints the command to run it manually
  * HTTPS: pass a certificate from your internal CA (-Cert / -Key). Without it the Hub runs on plain HTTP -
    acceptable only for a first LAN test.
  Afterwards: copy C:\SAPBotHub\keys\policy_signing.pub.pem to every laptop (C:\SAPBot\config\hub_public.pem) and
  set hub.url + hub.public_key_file there (Cockpit > Admin > Hub).
#>
param(
  [Parameter(Mandatory = $true)][string]$Admin,
  [string]$HubHome = "C:\SAPBotHub",
  [int]$Port = 8443,
  [string]$Cert = "",
  [string]$Key = ""
)
$ErrorActionPreference = "Stop"
$App = Join-Path $HubHome "app"
if (-not (Test-Path (Join-Path $App "pyproject.toml"))) { throw "Copy the SAPBot package to $App first" }
$venvPy = Join-Path $HubHome ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { py -3.12 -m venv "$HubHome\.venv" }
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -e "$App[hub]" --quiet
[Environment]::SetEnvironmentVariable("SAPBOT_HUB_HOME", $HubHome, "Machine")
$env:SAPBOT_HUB_HOME = $HubHome

& $venvPy -m sapbot.hub.app init --admin $Admin

$args_ = "-m sapbot.hub.app serve --port $Port"
if ($Cert -and $Key) { $args_ += " --cert `"$Cert`" --key `"$Key`"" }
if (Get-Command nssm -ErrorAction SilentlyContinue) {
  nssm install SAPBotHub $venvPy $args_
  nssm set SAPBotHub AppDirectory $HubHome
  $claude = Read-Host "Claude API key for the Hub proxy (Enter to skip; can be set later the same way)" -AsSecureString
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($claude))
  # The service runs as LocalSystem, so the key is given to the service environment (readable by server admins
  # only) instead of the interactive user's Credential Manager.
  if ($plain) { nssm set SAPBotHub AppEnvironmentExtra "SAPBOT_HUB_HOME=$HubHome" "ANTHROPIC_API_KEY=$plain" }
  else { nssm set SAPBotHub AppEnvironmentExtra "SAPBOT_HUB_HOME=$HubHome" }
  nssm set SAPBotHub AppStdout "$HubHome\logs\hub.out.log"
  nssm set SAPBotHub AppStderr "$HubHome\logs\hub.err.log"
  nssm set SAPBotHub Start SERVICE_AUTO_START
  nssm start SAPBotHub
  Write-Host "Service SAPBotHub installed and started on port $Port" -ForegroundColor Green
} else {
  Write-Host "NSSM not found - start manually:  $venvPy $args_" -ForegroundColor Yellow
}
New-NetFirewallRule -DisplayName "SAPBot Hub $Port" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow `
  -ErrorAction SilentlyContinue | Out-Null
Write-Host "Nightly backup: schedule a copy of $HubHome\data and $HubHome\keys (ADR B12)."
Write-Host "Claude API proxy key: set during install (service environment). To change it later:"
Write-Host "  nssm set SAPBotHub AppEnvironmentExtra SAPBOT_HUB_HOME=$HubHome ANTHROPIC_API_KEY=<key>; nssm restart SAPBotHub"
