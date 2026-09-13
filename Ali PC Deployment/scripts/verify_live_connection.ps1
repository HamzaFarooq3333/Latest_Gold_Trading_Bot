#Requires -Version 5.1
<#
  Verify the Ali PC bridge install and its link to the GCP live desk.
  Read-only: starts nothing. Use START_BOT.bat / start_bridge_stack.ps1 to start.
#>
[CmdletBinding()]
param(
  [string]$Root = "C:\onyxion-ali"
)

$ErrorActionPreference = "Continue"
$envPath = Join-Path $Root ".env"
$py = Join-Path $Root "venv\Scripts\python.exe"
$fail = 0

function Read-EnvValue($name) {
  if (-not (Test-Path $envPath)) { return $null }
  foreach ($raw in Get-Content $envPath -Encoding UTF8) {
    $line = $raw.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line -match "=") {
      $k, $v = $line.Split("=", 2)
      if ($k.Trim() -eq $name) { return $v.Trim().Trim('"').Trim("'") }
    }
  }
  return $null
}
function Ok($msg) { Write-Host "[PASS] $msg" -ForegroundColor Green }
function Bad($msg) { Write-Host "[FAIL] $msg" -ForegroundColor Red; $script:fail++ }
function Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor DarkCyan }

Write-Host "=== verify_live_connection root=$Root ===" -ForegroundColor Cyan
if (-not (Test-Path $envPath)) { Bad ".env missing: $envPath" } else { Ok ".env present" }
if (-not (Test-Path $py)) { Bad "venv python missing: $py (run INSTALL.ps1)" } else { Ok "venv python: $py" }

$lab = Read-EnvValue "ASIM_LAB_URL"
$gitDir = Read-EnvValue "BRIDGE_UPDATE_GIT"
$term = Read-EnvValue "ASIM_MT5_TERMINAL_PATH"
foreach ($pair in @(@("XTREND_SOURCE", "gaga"), @("SKIP_WORST_HOURS", "0"), @("FOCUS_BEST_HOURS", "0"), @("SKIP_WEEKENDS", "0"))) {
  $v = Read-EnvValue $pair[0]
  if ($v -eq $pair[1]) { Ok "$($pair[0])=$v" } else { Bad "$($pair[0]) should be $($pair[1]), got: $v" }
}
Info ("live config: ENTRY_EVERY_CANDLE=" + (Read-EnvValue "ENTRY_EVERY_CANDLE") + " TSL_ATR_MULT=" + (Read-EnvValue "TSL_ATR_MULT") +
      " HIST_THRESH=" + (Read-EnvValue "HIST_THRESH") + " VOLUME=" + (Read-EnvValue "VOLUME") + " MAX_SUPP=" + (Read-EnvValue "MAX_SUPP"))

if ($lab) { Ok "ASIM_LAB_URL=$lab" } else { Bad "ASIM_LAB_URL missing" }
if ($gitDir -and (Test-Path (Join-Path $gitDir ".git"))) { Ok "GitOps clone: $gitDir" } else { Bad "BRIDGE_UPDATE_GIT clone missing: $gitDir" }

if ($term -and (Test-Path $term)) {
  Ok "MT5 terminal: $term"
  foreach ($ind in @("OnyxionHistogram.mq5", "OnyxionXTrendProxy.mq5")) {
    $p = Join-Path (Split-Path $term) "MQL5\Indicators\$ind"
    if (Test-Path $p) { Ok "$ind installed" } else { Bad "missing $p" }
  }
} else {
  Bad "MT5 terminal missing: $term"
}

foreach ($proc in @(@("watchdog_exness", "Watchdog"), @("bridge_trader", "Bridge"), @("connection_monitor", "Connection monitor"), @("github_update_agent", "GitHub agent"))) {
  $hit = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match $proc[0] -and $_.CommandLine -like "*$Root*" } | Select-Object -First 1
  if ($hit) { Ok "$($proc[1]) running pid=$($hit.ProcessId)" } else { Bad "$($proc[1]) NOT running" }
}

if ((Test-Path $py) -and (Test-Path (Join-Path $Root "healthcheck_mt5.py"))) {
  Info "Running healthcheck_mt5.py ..."
  & $py (Join-Path $Root "healthcheck_mt5.py") --env $envPath
  if ($LASTEXITCODE -eq 0) { Ok "healthcheck exit 0" } else { Bad "healthcheck exit $LASTEXITCODE" }
}
if ((Test-Path $py) -and (Test-Path (Join-Path $Root "scripts\check_bridge_live.py"))) {
  & $py (Join-Path $Root "scripts\check_bridge_live.py")
  if ($LASTEXITCODE -eq 0) { Ok "desk heartbeat fresh (data flowing)" } else { Bad "desk heartbeat not fresh (exit $LASTEXITCODE)" }
}

Write-Host ""
if ($fail -eq 0) {
  Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
  exit 0
} else {
  Write-Host "$fail CHECK(S) FAILED" -ForegroundColor Red
  exit 1
}
