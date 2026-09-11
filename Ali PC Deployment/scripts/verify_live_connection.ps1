#Requires -Version 5.1
<#
  Verify Ali PC bridge is healthy and can reach the live GCP desk.
  Does not start services — use start_bridge_stack.ps1 after healthcheck PASS.
#>
[CmdletBinding()]
param(
  [ValidateSet("hamza", "ali")]
  [string]$Profile = "ali",
  [string]$Root = "",
  [string]$PythonPath = "C:\Program Files\Python312\python.exe"
)

$ErrorActionPreference = "Continue"
if (-not $Root) { $Root = "C:\onyxion-$Profile" }
$envPath = Join-Path $Root ".env"
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

Write-Host "=== verify_live_connection profile=$Profile root=$Root ===" -ForegroundColor Cyan

if (-not (Test-Path $envPath)) { Bad ".env missing: $envPath" } else { Ok ".env present" }

$lab = Read-EnvValue "ASIM_LAB_URL"
$bundle = Read-EnvValue "BRIDGE_UPDATE_URL"
$xt = Read-EnvValue "XTREND_SOURCE"
$skipW = Read-EnvValue "SKIP_WORST_HOURS"
$focusB = Read-EnvValue "FOCUS_BEST_HOURS"
$term = Read-EnvValue "ASIM_MT5_TERMINAL_PATH"

if ($lab -match "35\.253\.21\.246") { Ok "ASIM_LAB_URL=$lab" } else { Bad "ASIM_LAB_URL unexpected: $lab" }
if ($xt -eq "gaga") { Ok "XTREND_SOURCE=gaga" } else { Bad "XTREND_SOURCE should be gaga, got: $xt" }
if ($skipW -eq "0") { Ok "SKIP_WORST_HOURS=0" } else { Bad "SKIP_WORST_HOURS must be 0 (got $skipW)" }
if ($focusB -eq "0") { Ok "FOCUS_BEST_HOURS=0" } else { Bad "FOCUS_BEST_HOURS must be 0 (got $focusB)" }

if ($term -and (Test-Path $term)) {
  Ok "MT5 terminal: $term"
  $indH = Join-Path (Split-Path $term) "MQL5\Indicators\OnyxionHistogram.mq5"
  $indX = Join-Path (Split-Path $term) "MQL5\Indicators\OnyxionXTrendProxy.mq5"
  if (Test-Path $indH) { Ok "Histogram MQ5 installed" } else { Bad "Missing $indH" }
  if (Test-Path $indX) { Ok "X-Trend Gaga MQ5 installed" } else { Bad "Missing $indX" }
} else {
  Bad "MT5 terminal missing: $term"
}

$watch = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match "watchdog_exness" -and $_.CommandLine -like "*$Root*" } |
  Select-Object -First 1
if ($watch) { Ok "Watchdog running pid=$($watch.ProcessId)" } else { Bad "Watchdog NOT running — start_bridge_stack.ps1" }

if (-not (Test-Path $PythonPath)) {
  $PythonPath = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}
if ($PythonPath -and (Test-Path (Join-Path $Root "healthcheck_mt5.py"))) {
  Info "Running healthcheck_mt5.py ..."
  & $PythonPath (Join-Path $Root "healthcheck_mt5.py") --env $envPath --model ASIM
  if ($LASTEXITCODE -eq 0) { Ok "healthcheck exit 0" } else { Bad "healthcheck exit $LASTEXITCODE" }
} else {
  Bad "Cannot run healthcheck (python or script missing)"
}

if ($bundle) {
  try {
    $req = [System.Net.HttpWebRequest]::Create($bundle)
    $req.Method = "HEAD"
    $req.Timeout = 15000
    $req.ServerCertificateValidationCallback = { $true }
    $resp = $req.GetResponse()
    Ok "GCP bundle reachable: $bundle ($([int]$resp.StatusCode))"
    $resp.Close()
  } catch {
    Bad "GCP bundle unreachable: $($_.Exception.Message)"
    try { Info "Public IP: $(Invoke-RestMethod https://ifconfig.me -TimeoutSec 10)" } catch {}
  }
}

if ($lab) {
  try {
    $live = $lab.TrimEnd("/") + "/live"
    $req = [System.Net.HttpWebRequest]::Create($live)
    $req.Method = "GET"
    $req.Timeout = 15000
    $req.ServerCertificateValidationCallback = { $true }
    $resp = $req.GetResponse()
    Ok "Live desk HTTP OK: $live ($([int]$resp.StatusCode))"
    $resp.Close()
  } catch {
    Bad "Live desk unreachable: $($_.Exception.Message)"
  }
}

Write-Host ""
if ($fail -eq 0) {
  Write-Host "ALL CHECKS PASSED — keep Python/watchdog running; confirm /live feed timestamps in UTC." -ForegroundColor Green
  exit 0
} else {
  Write-Host "$fail CHECK(S) FAILED — fix before relying on live." -ForegroundColor Red
  exit 1
}
