#Requires -Version 5.1
<#
  One-shot setup for friend's MT5 laptop (profile ali).
  Run from repo on friend's PC after filling .env credentials.

  Installs bridge → registers 15-min auto-update → optional first update pull.
#>
[CmdletBinding()]
param(
  [string]$InstallRoot = "C:\onyxion-ali",
  [string]$BundleZip = "",
  [switch]$SkipAutoUpdate,
  [switch]$RunFirstUpdate
)

$ErrorActionPreference = "Stop"
$scripts = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== Onyxion friend bridge setup (ali) ===" -ForegroundColor Cyan

$installArgs = @{
  Profile = "ali"
  InstallRoot = $InstallRoot
}
if ($BundleZip) { $installArgs.BundleZip = $BundleZip }
& (Join-Path $scripts "install_bridge.ps1") @installArgs

$envPath = Join-Path $InstallRoot ".env"
if (-not (Test-Path $envPath)) {
  throw ".env missing at $envPath — copy env\.env.ali.example and fill MT5 login/password"
}

# Ensure update URL points at ali GCP VM
$envText = Get-Content $envPath -Raw -Encoding UTF8
if ($envText -notmatch "BRIDGE_UPDATE_URL=https?://") {
  $defaultUrl = "https://35.253.21.246/bridge/onyxion-bridge-bundle.zip"
  Add-Content $envPath "`nBRIDGE_UPDATE_URL=$defaultUrl"
  Write-Host "Added BRIDGE_UPDATE_URL=$defaultUrl" -ForegroundColor Yellow
}

if (-not $SkipAutoUpdate) {
  & (Join-Path $scripts "register_auto_update_task.ps1") -Profile ali -Root $InstallRoot -IntervalMinutes 15
}

if ($RunFirstUpdate) {
  & (Join-Path $scripts "auto_update_bridge.ps1") -Profile ali -Root $InstallRoot -Force
}

$mq5Installer = Join-Path $scripts "install_mq5_to_terminal.ps1"
if (Test-Path $mq5Installer) {
  & $mq5Installer -Profile ali -Root $InstallRoot
}

Write-Host ""
Write-Host "=== Next steps (friend) ===" -ForegroundColor Green
Write-Host "1. pip install -r $InstallRoot\requirements.txt"
Write-Host "2. Install portable MT5 to $InstallRoot\exness-mt5 and log in (demo)"
Write-Host "3. MQ5 copied by install_mq5_to_terminal.ps1 — recompile in MetaEditor if compile step failed"
Write-Host "4. python $InstallRoot\healthcheck_mt5.py --env $envPath --model ASIM"
Write-Host "5. powershell -ExecutionPolicy Bypass -File $InstallRoot\scripts\start_bridge_stack.ps1 -Profile ali"
Write-Host "6. powershell -ExecutionPolicy Bypass -File $InstallRoot\scripts\verify_auto_update.ps1 -Profile ali"
Write-Host "7. Open https://35.253.21.246/live (ali / 123451)"
Write-Host ""
Write-Host "Auto-update: every 15 min from BRIDGE_UPDATE_URL (no manual copies after dev runs publish_to_gcp.ps1)"
