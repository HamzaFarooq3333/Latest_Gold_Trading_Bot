#Requires -Version 5.1
<#
  Ali PC one-shot installer / upgrader.  Run from THIS folder:

    powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1

  * copies runtime\, scripts\, mq5\ and the bat helpers into C:\onyxion-ali
  * creates C:\onyxion-ali\venv and installs requirements
  * creates .env from env\.env.ali.example (never overwrites an existing .env)
  * copies the MQ5 indicators into the portable terminal if it is present
  * registers the four hidden scheduled tasks (scripts\register_tasks.ps1)

  Re-running is safe: existing .env and state\ are left untouched.
#>
[CmdletBinding()]
param(
  [string]$InstallRoot = "C:\onyxion-ali",
  [string]$LabUrl = "https://35.253.21.246",
  [switch]$SkipPip,
  [switch]$StartBridge
)

$ErrorActionPreference = "Stop"
$PackRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $PackRoot "runtime"
$Scripts = Join-Path $PackRoot "scripts"

Write-Host "=== Ali PC Deployment -> $InstallRoot ===" -ForegroundColor Cyan
Write-Host "Live desk: $LabUrl/live   Backtest: $LabUrl/" -ForegroundColor Cyan

foreach ($d in @($InstallRoot, "$InstallRoot\logs", "$InstallRoot\state", "$InstallRoot\scripts", "$InstallRoot\mq5", "$InstallRoot\backup")) {
  New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# Runtime (flat), scripts, mq5, helpers
Get-ChildItem $Runtime -File | Where-Object { $_.Name -notlike "test_*" } | ForEach-Object {
  Copy-Item $_.FullName (Join-Path $InstallRoot $_.Name) -Force
}
Copy-Item "$Scripts\*" "$InstallRoot\scripts\" -Recurse -Force
Copy-Item "$PackRoot\mq5\*" "$InstallRoot\mq5\" -Force
foreach ($f in @("fix_script_encoding.ps1", "START_BOT.bat", "CHECK_BOT.bat", "STOP_BOT.bat")) {
  Copy-Item (Join-Path $PackRoot $f) (Join-Path $InstallRoot $f) -Force
}

# .env: create from the template once; then only make sure the fixed keys exist.
$envPath = Join-Path $InstallRoot ".env"
if (-not (Test-Path $envPath)) {
  $text = Get-Content (Join-Path $PackRoot "env\.env.ali.example") -Raw -Encoding UTF8
  $text = $text -replace "https://35\.253\.21\.246", $LabUrl.TrimEnd("/")
  Set-Content -Path $envPath -Value $text -Encoding UTF8
  Write-Host "Created $envPath - fill in ASIM_MT5_PASSWORD." -ForegroundColor Yellow
} else {
  Write-Host "Keeping existing $envPath" -ForegroundColor DarkYellow
}
$envText = Get-Content $envPath -Raw -Encoding UTF8
$required = @{
  "ASIM_LAB_URL" = $LabUrl.TrimEnd("/")
  "ASIM_LAB_INSECURE" = "1"
  "BRIDGE_UPDATE_GIT" = "C:\onyxion-src\Latest_Gold_Trading_Bot"
  "BRIDGE_UPDATE_GIT_REMOTE" = "https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git"
  "ASIM_MT5_TERMINAL_PATH" = "$InstallRoot\exness-mt5\terminal64.exe"
  "ASIM_MT5_PORTABLE" = "1"
  "ASIM_MT5_SYMBOL" = "XAUUSDm"
}
foreach ($k in $required.Keys) {
  if ($envText -notmatch "(?m)^$k=") { $envText = $envText.TrimEnd() + "`r`n$k=$($required[$k])`r`n" }
}
Set-Content -Path $envPath -Value $envText -Encoding UTF8

# venv + packages (MetaTrader5 must be the 64-bit Python)
$venvPy = Join-Path $InstallRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  $base = $null
  foreach ($c in @("C:\Program Files\Python312\python.exe", "C:\Program Files\Python311\python.exe")) {
    if (Test-Path $c) { $base = $c; break }
  }
  if (-not $base) { $base = (Get-Command python -ErrorAction SilentlyContinue).Source }
  if (-not $base) { throw "Python 3.11+ 64-bit not found. Install from python.org and re-run." }
  Write-Host "=== creating venv with $base ===" -ForegroundColor Cyan
  & $base -m venv (Join-Path $InstallRoot "venv")
}
if (-not $SkipPip) {
  Write-Host "=== pip install ===" -ForegroundColor Cyan
  & $venvPy -m pip install --upgrade pip
  & $venvPy -m pip install -r (Join-Path $InstallRoot "requirements.txt")
}

# GitOps clone the update agent pulls from
$gitDir = "C:\onyxion-src\Latest_Gold_Trading_Bot"
if (-not (Test-Path (Join-Path $gitDir ".git"))) {
  if (Get-Command git -ErrorAction SilentlyContinue) {
    New-Item -ItemType Directory -Force -Path (Split-Path $gitDir) | Out-Null
    git clone https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git $gitDir
  } else {
    Write-Host "git not found - install Git and clone Latest_Gold_Trading_Bot to $gitDir" -ForegroundColor Yellow
  }
}

# MQ5 indicators into the portable terminal (if already installed)
if (Test-Path "$InstallRoot\exness-mt5\terminal64.exe") {
  & "$InstallRoot\scripts\install_mq5_to_terminal.ps1" -Profile ali -Root $InstallRoot
} else {
  Write-Host "MT5 not found at $InstallRoot\exness-mt5 - install the portable terminal there, then run scripts\install_mq5_to_terminal.ps1" -ForegroundColor Yellow
}

# Scheduled tasks
& "$InstallRoot\scripts\register_tasks.ps1" -Root $InstallRoot

Write-Host ""
Write-Host "=== MANUAL STEPS ===" -ForegroundColor Green
Write-Host "1. Install the portable Exness MT5 to $InstallRoot\exness-mt5, log in the DEMO account from .env, XAUUSDm M15, Algo Trading ON"
Write-Host "2. Compile mq5\*.mq5 in MetaEditor (F7) and attach OnyxionHistogram + OnyxionXTrendProxy (mq5\CHART_SETUP.md)"
Write-Host "3. Healthcheck:  $venvPy $InstallRoot\healthcheck_mt5.py --env $envPath"
Write-Host "4. Start:        $InstallRoot\START_BOT.bat   (or: Get-ScheduledTask 'OnyxionAli-*' | Start-ScheduledTask)"
Write-Host "5. Verify:       powershell -ExecutionPolicy Bypass -File $InstallRoot\scripts\verify_live_connection.ps1"
Write-Host "6. Browser:      $LabUrl/live"
if ($StartBridge) {
  & "$InstallRoot\scripts\start_bridge_stack.ps1" -Profile ali -Root $InstallRoot
}
