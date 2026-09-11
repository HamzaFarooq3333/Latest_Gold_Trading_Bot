#Requires -Version 5.1
[CmdletBinding()]
param(
  [ValidateSet("hamza", "ali")]
  [string]$Profile = "ali",
  [string]$InstallRoot = "",
  [string]$BundleZip = "",
  [switch]$RegisterAutoUpdate
)

$ErrorActionPreference = "Stop"
$profileRoot = if ($InstallRoot) { $InstallRoot } else { "C:\onyxion-$Profile" }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$mcRoot = Split-Path -Parent $here

New-Item -ItemType Directory -Force -Path $profileRoot, "$profileRoot\logs", "$profileRoot\state", "$profileRoot\scripts", "$profileRoot\mq5", "$profileRoot\backup" | Out-Null

if ($BundleZip -and (Test-Path $BundleZip)) {
  $tmp = Join-Path $env:TEMP ("onyxion-install-" + [guid]::NewGuid().ToString("n"))
  Expand-Archive -Path $BundleZip -DestinationPath $tmp -Force
  $bundleRoot = $tmp
  if (-not (Test-Path (Join-Path $tmp "bridge_trader.py"))) {
    $sub = Get-ChildItem $tmp -Directory | Select-Object -First 1
    if ($sub -and (Test-Path (Join-Path $sub.FullName "bridge_trader.py"))) { $bundleRoot = $sub.FullName }
  }
  Get-ChildItem $bundleRoot -File | ForEach-Object { Copy-Item $_.FullName $profileRoot -Force }
  if (Test-Path (Join-Path $bundleRoot "mq5")) {
    Copy-Item (Join-Path $bundleRoot "mq5\*") "$profileRoot\mq5\" -Recurse -Force
  }
  if (Test-Path (Join-Path $bundleRoot "scripts")) {
    Copy-Item (Join-Path $bundleRoot "scripts\*") "$profileRoot\scripts\" -Recurse -Force
  }
  Remove-Item $tmp -Recurse -Force
} else {
  $runtime = Join-Path $mcRoot "runtime"
  if (Test-Path (Join-Path $runtime "bridge_trader.py")) {
    @("bridge_trader.py", "watchdog_exness.py", "healthcheck_mt5.py", "requirements.txt", "mt5_live_engine.py") | ForEach-Object {
      Copy-Item (Join-Path $runtime $_) (Join-Path $profileRoot $_) -Force
    }
    Copy-Item (Join-Path $mcRoot "mq5\*") "$profileRoot\mq5\" -Force -ErrorAction SilentlyContinue
    $verSrc = Join-Path $mcRoot "bridge\VERSION.json"
    if (Test-Path $verSrc) { Copy-Item $verSrc $profileRoot -Force }
  } else {
    $repo = Split-Path -Parent $mcRoot
    $mig = Join-Path $repo "migration"
    @("bridge_trader.py", "watchdog_exness.py", "healthcheck_mt5.py", "requirements.txt") | ForEach-Object {
      Copy-Item (Join-Path $mig $_) (Join-Path $profileRoot $_) -Force
    }
    Copy-Item (Join-Path $repo "Google Console Deployment\app\mt5_live_engine.py") $profileRoot -Force
    Copy-Item (Join-Path $mig "Onyxion*.mq5") "$profileRoot\mq5\" -Force
    Copy-Item "$here\*.ps1" "$profileRoot\scripts\" -Force -Exclude "publish_bundle.ps1", "publish_to_gcp.ps1"
    $verSrc = Join-Path $mcRoot "bridge\VERSION.json"
    if (Test-Path $verSrc) { Copy-Item $verSrc $profileRoot -Force }
  }
  Copy-Item "$here\*.ps1" "$profileRoot\scripts\" -Force -Exclude "publish_bundle.ps1", "publish_to_gcp.ps1"
}

$envExample = Join-Path $mcRoot "env\.env.$Profile.example"
if (Test-Path $envExample) {
  $destEnv = Join-Path $profileRoot ".env"
  if (-not (Test-Path $destEnv)) {
    Copy-Item $envExample $destEnv
    Write-Host "Created $destEnv — fill in MT5 credentials." -ForegroundColor Yellow
  }
}

if ($RegisterAutoUpdate -or $Profile -eq "ali") {
  & (Join-Path $here "register_auto_update_task.ps1") -Profile $Profile -Root $profileRoot
}

Write-Host "Installed bridge to $profileRoot" -ForegroundColor Green
Write-Host "Next: pip install -r $profileRoot\requirements.txt"
Write-Host "Next: edit $profileRoot\.env (MT5 login + BRIDGE_UPDATE_URL)"
Write-Host "Next: start_bridge_stack.ps1 -Profile $Profile -Root $profileRoot"
