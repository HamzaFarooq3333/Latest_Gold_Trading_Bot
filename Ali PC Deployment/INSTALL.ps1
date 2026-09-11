#Requires -Version 5.1
<#
  Ali PC one-shot installer.
  Run in elevated PowerShell from THIS folder:

    cd "...\Ali PC Deployment"
    powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1

  Optional:
    -InstallMt5     Download/install portable MetaTrader 5 under C:\onyxion-ali\exness-mt5
    -StartBridge    Start watchdog after healthcheck (only if MT5 already logged in)
    -SkipPip        Skip pip install
#>
[CmdletBinding()]
param(
  [string]$InstallRoot = "C:\onyxion-ali",
  [string]$LabUrl = "https://35.253.21.246",
  [switch]$InstallMt5,
  [switch]$StartBridge,
  [switch]$SkipPip
)

$ErrorActionPreference = "Stop"
$PackRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $PackRoot "runtime"
$Scripts = Join-Path $PackRoot "scripts"
$Mq5 = Join-Path $PackRoot "mq5"
$EnvExample = Join-Path $PackRoot "env\.env.ali.example"

Write-Host "=== Ali PC Deployment → $InstallRoot ===" -ForegroundColor Cyan
Write-Host "Live desk: $LabUrl/live   Backtest: $LabUrl/" -ForegroundColor Cyan

# Folders
New-Item -ItemType Directory -Force -Path @(
  $InstallRoot,
  "$InstallRoot\logs",
  "$InstallRoot\state",
  "$InstallRoot\scripts",
  "$InstallRoot\mq5",
  "$InstallRoot\backup",
  "$InstallRoot\installers"
) | Out-Null

# Runtime files
$need = @(
  "bridge_trader.py", "mt5_live_engine.py", "watchdog_exness.py",
  "healthcheck_mt5.py", "requirements.txt", "VERSION.json"
)
foreach ($f in $need) {
  $src = Join-Path $Runtime $f
  if (-not (Test-Path $src)) { throw "Missing pack file: $src" }
  Copy-Item $src (Join-Path $InstallRoot $f) -Force
}

# Scripts + mq5
Copy-Item "$Scripts\*.ps1" "$InstallRoot\scripts\" -Force
if (Test-Path "$Scripts\optional") {
  New-Item -ItemType Directory -Force -Path "$InstallRoot\scripts\optional" | Out-Null
  Copy-Item "$Scripts\optional\*" "$InstallRoot\scripts\optional\" -Force -ErrorAction SilentlyContinue
}
Copy-Item "$Mq5\*" "$InstallRoot\mq5\" -Force

# .env
$envPath = Join-Path $InstallRoot ".env"
if (-not (Test-Path $envPath)) {
  $text = Get-Content $EnvExample -Raw -Encoding UTF8
  $text = $text -replace "https://35\.253\.21\.246", $LabUrl.TrimEnd("/")
  Set-Content -Path $envPath -Value $text -Encoding UTF8
  Write-Host "Created $envPath from example (update password/server if needed)." -ForegroundColor Yellow
} else {
  Write-Host "Keeping existing $envPath" -ForegroundColor DarkYellow
}

# Ensure critical URL keys exist / match LabUrl
$envText = Get-Content $envPath -Raw -Encoding UTF8
$updates = @{
  "ASIM_LAB_URL" = $LabUrl.TrimEnd("/")
  "ASIM_LAB_INSECURE" = "1"
  "BRIDGE_UPDATE_URL" = ($LabUrl.TrimEnd("/") + "/bridge/onyxion-bridge-bundle.zip")
  "BRIDGE_PROFILE" = "ali"
  "XTREND_SOURCE" = "gaga"
  "XTREND_GATE" = "1"
  "XTREND_GATE_SUPP" = "0"
  "TRAIL_ENTRY_BAR" = "1"
  "TRAIL_EVERY_CANDLE" = "1"
  "ENTRY_BAR_MODE" = "defer"
  # Best/worst hour filters MUST stay OFF for Ali live (trade all hours)
  "SKIP_WORST_HOURS" = "0"
  "FOCUS_BEST_HOURS" = "0"
  "SKIP_WEEKENDS" = "0"
  "ASIM_MT5_TERMINAL_PATH" = "$InstallRoot\exness-mt5\terminal64.exe"
  "ASIM_MT5_PORTABLE" = "1"
  "ASIM_MT5_SYMBOL" = "XAUUSDm"
  "EXPECTED_MT5_LOGIN" = "472640728"
  "EXPECTED_MT5_SYMBOL" = "XAUUSDm"
}
foreach ($k in $updates.Keys) {
  $v = $updates[$k]
  if ($envText -match "(?m)^$k=") {
    $envText = [regex]::Replace($envText, "(?m)^$k=.*$", "$k=$v")
  } else {
    $envText = $envText.TrimEnd() + "`r`n$k=$v`r`n"
  }
}
Set-Content -Path $envPath -Value $envText -Encoding UTF8

# Python packages
if (-not $SkipPip) {
  Write-Host "=== pip install ===" -ForegroundColor Cyan
  $py = $null
  foreach ($c in @(
      "C:\Program Files\Python312\python.exe",
      "C:\Program Files\Python311\python.exe",
      "C:\Python312\python.exe"
    )) {
    if (Test-Path $c) { $py = $c; break }
  }
  if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
  if (-not $py) { throw "Python 3.11+ 64-bit not found. Install from python.org and re-run." }
  & $py -m pip install --upgrade pip
  & $py -m pip install -r (Join-Path $InstallRoot "requirements.txt")
}

# Optional MT5 install
if ($InstallMt5) {
  Write-Host "=== Installing portable MT5 ===" -ForegroundColor Cyan
  $mt5Script = Join-Path $InstallRoot "scripts\optional\install_exness_terminal.ps1"
  & $mt5Script `
    -InstallerPath "$InstallRoot\installers\mt5setup.exe" `
    -InstallRoot "$InstallRoot\exness-mt5"
}

# Copy MQ5 into terminal if present
$term = Join-Path $InstallRoot "exness-mt5\terminal64.exe"
if (Test-Path $term) {
  & (Join-Path $InstallRoot "scripts\install_mq5_to_terminal.ps1") -Profile ali -Root $InstallRoot
} else {
  Write-Host "MT5 not found yet at $term — install terminal, then re-run install_mq5_to_terminal.ps1" -ForegroundColor Yellow
}

# Auto-update every 15 min
& (Join-Path $InstallRoot "scripts\register_auto_update_task.ps1") -Profile ali -Root $InstallRoot -IntervalMinutes 15

# Connectivity smoke test to desk
Write-Host "=== Desk connectivity ===" -ForegroundColor Cyan
try {
  $bundle = "$($LabUrl.TrimEnd('/'))/bridge/onyxion-bridge-bundle.zip"
  $req = [System.Net.HttpWebRequest]::Create($bundle)
  $req.Method = "HEAD"
  $req.Timeout = 15000
  $req.ServerCertificateValidationCallback = { $true }
  $resp = $req.GetResponse()
  Write-Host "Bundle OK: $bundle  ($([int]$resp.StatusCode))" -ForegroundColor Green
  $resp.Close()
} catch {
  Write-Host "WARN: cannot reach bridge bundle. Firewall must allow THIS PC public IP on GCP :443." -ForegroundColor Red
  Write-Host "  Check https://ifconfig.me and tell Onyxion to whitelist /32" -ForegroundColor Red
  Write-Host "  Error: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "=== MANUAL STEPS (required) ===" -ForegroundColor Green
Write-Host "1. Open MT5: $InstallRoot\exness-mt5\terminal64.exe"
Write-Host "2. Login DEMO: 472640728 / (password in .env) / Exness-MT5Trial16"
Write-Host "3. Market Watch → XAUUSDm | Chart M15 | Algo Trading ON (green)"
Write-Host "4. MetaEditor: compile mq5 under MQL5\Indicators and MQL5\Experts (F7)"
Write-Host "5. Attach OnyxionHistogram (subwindow) + OnyxionXTrendProxy/KJ Gaga (main) — see mq5\CHART_SETUP.md"
Write-Host "6. Healthcheck:"
Write-Host "     python $InstallRoot\healthcheck_mt5.py --env $envPath --model ASIM"
Write-Host "7. Start bridge (keep Python running):"
Write-Host "     powershell -ExecutionPolicy Bypass -File $InstallRoot\scripts\start_bridge_stack.ps1 -Profile ali"
Write-Host "8. Verify:"
Write-Host "     powershell -ExecutionPolicy Bypass -File $InstallRoot\scripts\verify_live_connection.ps1 -Profile ali"
Write-Host "9. Browser: $LabUrl/live   login ali / 123451  (chart clock = UTC, same as MT5)"
Write-Host "   Backtest UI: $LabUrl/"
Write-Host "10. Connection guide: CONNECT_LIVE.md · Claude prompt: prompts\CLAUDE_SETUP_PROMPT.md"
Write-Host ""
Write-Host "Engine: primary body+XT · SUPP raw wick · TRAIL_ENTRY_BAR=1 · hour filters OFF" -ForegroundColor DarkCyan
Write-Host ""
if ($StartBridge) {
  Write-Host "=== Starting bridge stack ===" -ForegroundColor Cyan
  & (Join-Path $InstallRoot "scripts\start_bridge_stack.ps1") -Profile ali -Root $InstallRoot
}

Write-Host "Done. See START_HERE.md and prompts\CLAUDE_SETUP_PROMPT.md" -ForegroundColor Cyan
