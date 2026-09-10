#Requires -Version 5.1
<#
  Bootstrap Onyxion bridge on a fresh Windows PC WITHOUT a zip file from dev.
  Downloads the live bundle from GCP BRIDGE_UPDATE_URL and installs to C:\onyxion-{profile}.

  Usage (ali — friend's laptop):
    powershell -ExecutionPolicy Bypass -File .\bootstrap_from_gcp.ps1 -Profile ali

  Requires: Python 3.11+, Administrator for scheduled task registration.
#>
[CmdletBinding()]
param(
  [ValidateSet("hamza", "ali")]
  [string]$Profile = "ali",
  [string]$InstallRoot = "",
  [switch]$SkipAutoUpdate,
  [switch]$SkipMq5
)

$ErrorActionPreference = "Stop"
$scripts = Split-Path -Parent $MyInvocation.MyCommand.Path
$mcRoot = Split-Path -Parent $scripts
if (-not $InstallRoot) { $InstallRoot = "C:\onyxion-$Profile" }

$defaultUrls = @{
  hamza = "https://35.232.76.12/bridge/onyxion-bridge-bundle.zip"
  ali   = "https://35.253.21.246/bridge/onyxion-bridge-bundle.zip"
}
$updateUrl = $defaultUrls[$Profile]

Write-Host "=== Onyxion bootstrap from GCP ($Profile) ===" -ForegroundColor Cyan
Write-Host "Install root: $InstallRoot"
Write-Host "Bundle URL:   $updateUrl"

New-Item -ItemType Directory -Force -Path $InstallRoot, "$InstallRoot\logs", "$InstallRoot\scripts" | Out-Null

# Minimal .env so download can use ASIM_LAB_INSECURE
$envPath = Join-Path $InstallRoot ".env"
if (-not (Test-Path $envPath)) {
  $example = Join-Path $mcRoot "env\.env.$Profile.example"
  if (Test-Path $example) {
    Copy-Item $example $envPath
  } else {
    @"
ASIM_LAB_URL=$($defaultUrls[$Profile] -replace '/bridge/.*','')
ASIM_LAB_INSECURE=1
BRIDGE_UPDATE_URL=$updateUrl
BRIDGE_PROFILE=$Profile
SKIP_WORST_HOURS=0
FOCUS_BEST_HOURS=0
SKIP_WEEKENDS=0
"@ | Set-Content $envPath -Encoding UTF8
  }
  Write-Host "Created $envPath — fill MT5 login/password/server before healthcheck." -ForegroundColor Yellow
}

# Ensure update URL in .env
$envText = Get-Content $envPath -Raw -Encoding UTF8
if ($envText -notmatch "BRIDGE_UPDATE_URL=") {
  Add-Content $envPath "BRIDGE_UPDATE_URL=$updateUrl"
}

# Download bundle (TLS bypass for self-signed GCP cert)
$zipPath = Join-Path $env:TEMP "onyxion-bootstrap.zip"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$prev = [System.Net.ServicePointManager]::ServerCertificateValidationCallback
[System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
try {
  Write-Host "Downloading bundle..."
  Invoke-WebRequest -Uri $updateUrl -OutFile $zipPath -UseBasicParsing
} finally {
  [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $prev
}

& (Join-Path $scripts "install_bridge.ps1") -Profile $Profile -InstallRoot $InstallRoot -BundleZip $zipPath
Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

# Seed local changelog
$changelog = Join-Path $InstallRoot "CHANGELOG.md"
if (-not (Test-Path $changelog)) {
  $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss UTC")
  @"
# Onyxion Bridge — Local Update Changelog ($Profile)

Machine-local audit trail: every bundle check and every applied update from GCP.

## $ts — INSTALLED

- profile: **$Profile**
- source: bootstrap_from_gcp.ps1
- bundle: ``$updateUrl``

---
"@ | Set-Content $changelog -Encoding UTF8
}

if (-not $SkipAutoUpdate) {
  & (Join-Path $scripts "register_auto_update_task.ps1") -Profile $Profile -Root $InstallRoot -IntervalMinutes 15
  & (Join-Path $scripts "auto_update_bridge.ps1") -Profile $Profile -Root $InstallRoot -Force
}

if (-not $SkipMq5) {
  $mq5 = Join-Path $scripts "install_mq5_to_terminal.ps1"
  if (Test-Path $mq5) { & $mq5 -Profile $Profile -Root $InstallRoot }
}

Write-Host ""
Write-Host "=== Bootstrap complete ===" -ForegroundColor Green
Write-Host "1. Edit $envPath (MT5 demo login, password, server)"
Write-Host "2. pip install -r $InstallRoot\requirements.txt"
Write-Host "3. Install MT5 portable to $InstallRoot\exness-mt5, login demo, XAUUSDm M15"
Write-Host "4. python $InstallRoot\healthcheck_mt5.py --env $envPath --model ASIM"
Write-Host "5. powershell -ExecutionPolicy Bypass -File $InstallRoot\scripts\start_bridge_stack.ps1 -Profile $Profile"
Write-Host "6. powershell -ExecutionPolicy Bypass -File $InstallRoot\scripts\verify_auto_update.ps1 -Profile $Profile"
Write-Host "7. Changelog: $changelog"
$desk = if ($Profile -eq "hamza") { "https://35.232.76.12/live" } else { "https://35.253.21.246/live" }
Write-Host "8. Dashboard: $desk ($Profile / 123451)"
Write-Host "9. Verify: powershell -File $InstallRoot\scripts\verify_live_connection.ps1 -Profile $Profile"
