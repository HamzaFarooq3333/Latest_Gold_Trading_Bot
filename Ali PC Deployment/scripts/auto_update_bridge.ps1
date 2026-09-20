#Requires -Version 5.1
<#
  Candle-safe bridge update from the Latest_Gold_Trading_Bot clone.

  Called by github_update_agent.py (with -Force -SkipCandleWait after it has
  already waited for the M15 close and written the deploy-skip marker) or by
  hand:  auto_update_bridge.ps1 -Profile ali -Root C:\onyxion-ali -Force

  Steps: git pull --ff-only -> fingerprint compare -> safety gate ->
  backup -> (wait for M15 close + write state\deploy_skip_bar.json) ->
  copy runtime + scripts + mq5 -> restart watchdog + bridge.
  Never touches .env or state\ and never flattens open positions: the skip
  marker only suppresses NEW entries on the first candle after the restart.
#>
[CmdletBinding()]
param(
  [ValidateSet("hamza", "ali")]
  [string]$Profile = "ali",
  [string]$Root = "",
  [switch]$Force,
  [switch]$SkipCandleWait
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = "C:\onyxion-$Profile" }
$logDir = Join-Path $Root "logs"
$backupDir = Join-Path $Root "backup"
$logFile = Join-Path $logDir "auto_update.log"
$historyFile = Join-Path $logDir "update_history.log"
# Files copied flat from "Ali PC Deployment\runtime" into the install root.
$trackFiles = @(
  "bridge_trader.py", "mt5_live_engine.py", "watchdog_exness.py", "healthcheck_mt5.py",
  "connection_monitor.py", "github_update_agent.py", "safety_gate_check.py",
  "requirements.txt", "VERSION.json"
)
# A change to any of these needs the running bridge restarted.
$restartOn = "bridge_trader|mt5_live_engine|watchdog|connection_monitor|github_update_agent"

New-Item -ItemType Directory -Force -Path $logDir, $backupDir, (Join-Path $Root "scripts") | Out-Null

function Write-Log($msg) {
  $line = "{0} {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss"), $msg
  Add-Content -Path $logFile -Value $line -Encoding UTF8
  Write-Host $line
}

function Write-History($msg) {
  $line = "{0} {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss UTC"), $msg
  Add-Content -Path $historyFile -Value $line -Encoding UTF8
}

function Read-EnvValue($name) {
  $envPath = Join-Path $Root ".env"
  if (-not (Test-Path $envPath)) { return [Environment]::GetEnvironmentVariable($name) }
  foreach ($raw in Get-Content $envPath -Encoding UTF8) {
    $line = $raw.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line -match "=") {
      $k, $v = $line.Split("=", 2)
      if ($k.Trim() -eq $name) { return $v.Trim().Trim('"').Trim("'") }
    }
  }
  return [Environment]::GetEnvironmentVariable($name)
}

function Get-BundleFingerprint($dir) {
  $parts = @()
  foreach ($name in $trackFiles) {
    $p = Join-Path $dir $name
    if (Test-Path $p) { $parts += (Get-FileHash $p -Algorithm SHA256).Hash }
  }
  if (-not $parts.Count) { return "" }
  return ($parts -join "|")
}

function Stop-WatchdogForRoot($rootPath) {
  # Plain wildcard match. The previous version ran the root through
  # [regex]::Escape and then used -like, so "C:\\onyxion-ali" never matched
  # "C:\onyxion-ali": no process was ever stopped and every "restart" was a
  # no-op that left the old code running.
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object {
      $_.CommandLine -and ($_.CommandLine -like "*$rootPath*") -and
      ($_.CommandLine -like "*watchdog_exness.py*" -or $_.CommandLine -like "*bridge_trader.py*")
    } |
    ForEach-Object {
      Write-Log "stop pid=$($_.ProcessId) $($_.CommandLine.Substring(0, [Math]::Min(120, $_.CommandLine.Length)))"
      Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
  Start-Sleep -Seconds 3
}

function Start-WatchdogForProfile($profileName, $rootPath) {
  $repair = Join-Path $rootPath "fix_script_encoding.ps1"
  if (Test-Path $repair) { & $repair -Root $rootPath | Out-Null }
  $starter = Join-Path $rootPath "scripts\start_bridge_stack.ps1"
  if (-not (Test-Path $starter)) { throw "start_bridge_stack.ps1 not found under $rootPath\scripts" }
  & $starter -Profile $profileName -Root $rootPath
}

function Get-UtcM15Open([DateTime]$utcNow) {
  $minute = [Math]::Floor($utcNow.Minute / 15) * 15
  return [DateTime]::SpecifyKind((New-Object DateTime($utcNow.Year, $utcNow.Month, $utcNow.Day, $utcNow.Hour, $minute, 0)), [DateTimeKind]::Utc)
}

function Wait-ForCurrentM15Close {
  $now = [DateTime]::UtcNow
  $currentOpen = Get-UtcM15Open $now
  $closeAt = $currentOpen.AddMinutes(15)
  $waitSec = [Math]::Ceiling(($closeAt - [DateTime]::UtcNow).TotalSeconds) + 3
  if ($waitSec -lt 3) { $waitSec = 3 }
  if ($waitSec -gt 960) { $waitSec = 960 }
  Write-Log ("waiting for M15 close bar_open={0:yyyy-MM-ddTHH:mm:ss}Z skip_next={1:yyyy-MM-ddTHH:mm:ss}Z wait_sec={2}" -f $currentOpen, $closeAt, $waitSec)
  Start-Sleep -Seconds $waitSec
  return @{
    WaitedCloseBar = ($currentOpen.ToString("yyyy-MM-ddTHH:mm:ss") + "Z")
    SkipBarTime    = ($closeAt.ToString("yyyy-MM-ddTHH:mm:ss") + "Z")
  }
}

function Write-DeploySkipMarker([string]$RootPath, [string]$SkipBarTime, [string]$WaitedCloseBar, [string]$Version) {
  $stateDir = Join-Path $RootPath "state"
  New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
  $marker = Join-Path $stateDir "deploy_skip_bar.json"
  @{
    skip_bar_time = $SkipBarTime; waited_close_bar = $WaitedCloseBar; version = $Version
    reason = "auto_update_skip_next_bar"; no_flatten = $true; preserve_positions = $true; preserve_sl = $true
    written_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  } | ConvertTo-Json -Depth 4 | Set-Content -Path $marker -Encoding UTF8
  Write-Log "wrote deploy skip marker skip=$SkipBarTime (entries only; no flatten)"
}

function Invoke-SafetyGate([string]$CheckRoot) {
  $gatePs1 = Join-Path $PSScriptRoot "safety_gate.ps1"
  if (-not (Test-Path $gatePs1)) {
    Write-Log "WARN: safety_gate.ps1 missing; continuing without gate"
    return $true
  }
  Write-Log "safety_gate check root=$CheckRoot"
  & powershell -NoProfile -ExecutionPolicy Bypass -File $gatePs1 -Root $CheckRoot -NoDashboard
  if ($LASTEXITCODE -ne 0) {
    Write-Log "ERROR: safety_gate FAILED - refusing to copy live runtime"
    @{ ok = $false; reason = "safety_gate_failed"; checked_root = $CheckRoot; utc = (Get-Date).ToUniversalTime().ToString("o") } |
      ConvertTo-Json -Depth 4 | Set-Content (Join-Path $Root "state\last_update_result.json") -Encoding UTF8
    return $false
  }
  Write-Log "safety_gate PASS"
  return $true
}

Write-Log "auto_update start profile=$Profile root=$Root force=$Force skipWait=$SkipCandleWait"

$gitDir = Read-EnvValue "BRIDGE_UPDATE_GIT"
$gitRemote = Read-EnvValue "BRIDGE_UPDATE_GIT_REMOTE"
if (-not $gitDir) { $gitDir = "C:\onyxion-src\Latest_Gold_Trading_Bot" }
if (-not $gitRemote) { $gitRemote = "https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git" }
if (-not (Test-Path (Join-Path $gitDir ".git"))) {
  Write-Log "ERROR: no git clone at $gitDir - clone Latest_Gold_Trading_Bot there first"
  exit 1
}
$pack = Join-Path $gitDir "Ali PC Deployment"
$runtime = Join-Path $pack "runtime"

try {
  Push-Location $gitDir
  git remote set-url origin $gitRemote 2>$null
  git pull --ff-only 2>&1 | ForEach-Object { Write-Log "git: $_" }
  $sha = (git rev-parse --short HEAD 2>$null)
  Pop-Location
  if (-not (Test-Path (Join-Path $runtime "bridge_trader.py"))) {
    Write-Log "ERROR: $runtime has no bridge_trader.py"
    exit 1
  }

  $newFp = Get-BundleFingerprint $runtime
  $oldFp = Get-BundleFingerprint $Root
  $incomingVersion = "unknown"
  try {
    $vo = Get-Content (Join-Path $runtime "VERSION.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($vo.version) { $incomingVersion = [string]$vo.version }
  } catch { }

  if (-not $Force -and $newFp -and $oldFp -and $newFp -eq $oldFp) {
    Write-Log "no change (fingerprint match) sha=$sha"
    exit 0
  }
  if (-not (Invoke-SafetyGate -CheckRoot $gitDir)) { exit 2 }

  Write-Log "update detected sha=$sha version=$incomingVersion - candle-safe apply (state/ and .env preserved; never flattens)"
  foreach ($name in $trackFiles) {
    $src = Join-Path $Root $name
    if (Test-Path $src) { Copy-Item $src (Join-Path $backupDir $name) -Force }
  }

  if ($SkipCandleWait) {
    $timing = @{ WaitedCloseBar = ""; SkipBarTime = "" }
    $skipPath = Join-Path $Root "state\deploy_skip_bar.json"
    if (Test-Path $skipPath) {
      try {
        $m = Get-Content $skipPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $timing.WaitedCloseBar = [string]$m.waited_close_bar
        $timing.SkipBarTime = [string]$m.skip_bar_time
      } catch { }
    }
    Write-Log "SkipCandleWait=1 using existing skip marker skip=$($timing.SkipBarTime)"
  } else {
    $timing = Wait-ForCurrentM15Close
    Write-DeploySkipMarker -RootPath $Root -SkipBarTime $timing.SkipBarTime -WaitedCloseBar $timing.WaitedCloseBar -Version $incomingVersion
  }

  $restart = $false
  $copied = @()
  $unchanged = @()
  $stamp = Get-Date
  foreach ($name in $trackFiles) {
    $from = Join-Path $runtime $name
    if (-not (Test-Path $from)) { continue }
    $to = Join-Path $Root $name
    if ((Test-Path $to) -and ((Get-FileHash $to -Algorithm SHA256).Hash -eq (Get-FileHash $from -Algorithm SHA256).Hash)) {
      # Identical content: leave the file alone, mtime included. Re-copying and
      # re-stamping it made the connection monitor's stale-code check (file
      # newer than the running bridge) restart the bridge two minutes after
      # every push that did not touch the runtime - outside the candle-safe
      # window this script waited for.
      $unchanged += $name
      continue
    }
    if ((Test-Path $to) -and $name -match $restartOn) { $restart = $true }
    Copy-Item $from $to -Force
    # Copy-Item keeps the source mtime (the git checkout time). The connection
    # monitor's stale-code check compares file mtime with the bridge's start
    # time, so stamp "now" or an update pulled hours ago looks already loaded.
    (Get-Item $to).LastWriteTime = $stamp
    $copied += $name
  }
  if ($unchanged.Count) { Write-Log "unchanged (not copied): $($unchanged -join ', ')" }
  Copy-Item (Join-Path $pack "scripts\*") (Join-Path $Root "scripts") -Recurse -Force
  $copied += "scripts/*"
  foreach ($extra in @("fix_script_encoding.ps1", "START_BOT.bat", "CHECK_BOT.bat", "STOP_BOT.bat")) {
    $p = Join-Path $pack $extra
    if (Test-Path $p) { Copy-Item $p (Join-Path $Root $extra) -Force }
  }
  if (Test-Path (Join-Path $pack "mq5")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "mq5") | Out-Null
    Copy-Item (Join-Path $pack "mq5\*") (Join-Path $Root "mq5") -Recurse -Force
    $copied += "mq5/*"
  }
  Write-Log "copied: $($copied -join ', ') (state/ and .env untouched)"

  if ($restart) {
    Write-Log "restarting bridge stack (positions stay open on MT5)"
    Stop-WatchdogForRoot $Root
    Start-WatchdogForProfile $Profile $Root
  } else {
    Write-Log "runtime unchanged - no restart needed"
  }

  $restartFlag = if ($restart) { "yes" } else { "no" }
  Write-Log "update complete sha=$sha restart=$restartFlag waited_close=$($timing.WaitedCloseBar) skip_bar=$($timing.SkipBarTime)"
  Write-History "sha=$sha version=$incomingVersion restart=$restartFlag waited_close=$($timing.WaitedCloseBar) skip_bar=$($timing.SkipBarTime) files=$($copied -join ', ')"
  @{
    ok = $true; sha = $sha; version = $incomingVersion; restart = $restart
    waited_close_bar = $timing.WaitedCloseBar; skip_bar_time = $timing.SkipBarTime
    fingerprint = $newFp; files = $copied; utc = (Get-Date).ToUniversalTime().ToString("o")
  } | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $Root "state\last_update_result.json") -Encoding UTF8
} catch {
  Write-Log "ERROR: $($_.Exception.Message)"
  exit 1
}
