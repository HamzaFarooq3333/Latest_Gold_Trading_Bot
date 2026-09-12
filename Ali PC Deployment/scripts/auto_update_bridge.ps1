#Requires -Version 5.1
<#
  Pull latest bridge bundle every 15 minutes (Windows Scheduled Task).
  Reads BRIDGE_UPDATE_URL from C:\onyxion-{profile}\.env (or git pull).

  On change: backup → copy Python + scripts → restart watchdog (MT5 stays open).
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
$changelogFile = Join-Path $Root "CHANGELOG.md"
$trackFiles = @(
  "bridge_trader.py",
  "mt5_live_engine.py",
  "watchdog_exness.py",
  "healthcheck_mt5.py",
  "connection_monitor.py",
  "github_update_agent.py",
  "safety_gate_check.py",
  "requirements.txt",
  "VERSION.json"
)

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

function Ensure-ChangelogHeader {
  if (Test-Path $changelogFile) { return }
  $header = @"
# Onyxion Bridge — Local Update Changelog ($Profile)

Machine-local audit trail: every bundle check and every applied update from GCP.
Dev publishes via ``publish_to_gcp.ps1``; this file is written by ``auto_update_bridge.ps1``.

| Field | Location |
|-------|----------|
| Routine log | ``logs\auto_update.log`` |
| One-line history | ``logs\update_history.log`` |
| This file | ``CHANGELOG.md`` (human-readable) |

---

"@
  Set-Content -Path $changelogFile -Value $header -Encoding UTF8
}

function Write-Changelog {
  param(
    [string]$Action,
    [string]$Version = "unknown",
    [string]$Detail = ""
  )
  Ensure-ChangelogHeader
  $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss UTC")
  $entry = @"

## $ts — $Action

- profile: **$Profile**
- version: ``$Version``
$Detail

"@
  Add-Content -Path $changelogFile -Value $entry -Encoding UTF8
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

function Find-BundleRoot($dir) {
  if (Test-Path (Join-Path $dir "bridge_trader.py")) { return $dir }
  foreach ($child in Get-ChildItem $dir -Directory -ErrorAction SilentlyContinue) {
    $hit = Find-BundleRoot $child.FullName
    if ($hit) { return $hit }
  }
  return $null
}

function Test-LabInsecureTls() {
  $v = Read-EnvValue "ASIM_LAB_INSECURE"
  if ($null -eq $v -or [string]::IsNullOrWhiteSpace($v)) { return $true }
  return $v.Trim().ToLower() -notin @("0", "false", "no")
}

function Invoke-LabWebRequest {
  param([string]$Uri, [string]$OutFile)
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
  if ($curl -and (Test-LabInsecureTls)) {
    Write-Log "download via curl.exe (GCP TLS renegotiation workaround)"
    & curl.exe -k -s -L --connect-timeout 60 --max-time 300 -o $OutFile $Uri
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $OutFile) -or (Get-Item $OutFile).Length -lt 100) {
      throw "curl download failed exit=$LASTEXITCODE"
    }
    return
  }
  if (Test-LabInsecureTls) {
    Write-Log "download (ASIM_LAB_INSECURE=1, skip cert verify for GCP self-signed)"
    $prev = [System.Net.ServicePointManager]::ServerCertificateValidationCallback
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
    try {
      Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing
    } finally {
      [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $prev
    }
  } else {
    Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing
  }
}

function Stop-WatchdogForRoot($rootPath) {
  $escaped = [regex]::Escape($rootPath)
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object {
      $_.CommandLine -and (
        ($_.CommandLine -like "*watchdog_exness.py*" -and $_.CommandLine -like "*$escaped*") -or
        ($_.CommandLine -like "*bridge_trader.py*" -and $_.CommandLine -like "*$escaped*")
      )
    } |
    ForEach-Object {
      Write-Log "stop pid=$($_.ProcessId) $($_.CommandLine.Substring(0, [Math]::Min(120, $_.CommandLine.Length)))"
      Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
  Start-Sleep -Seconds 2
}

function Start-WatchdogForProfile($profileName, $rootPath) {
  $starter = Join-Path $rootPath "scripts\start_bridge_stack.ps1"
  if (-not (Test-Path $starter)) {
    $starter = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "start_bridge_stack.ps1"
  }
  if (Test-Path $starter) {
    & $starter -Profile $profileName -Root $rootPath
  } else {
    throw "start_bridge_stack.ps1 not found"
  }
}

function Get-UtcM15Open([DateTime]$utcNow) {
  $minute = [Math]::Floor($utcNow.Minute / 15) * 15
  return [DateTime]::SpecifyKind(
    (New-Object DateTime($utcNow.Year, $utcNow.Month, $utcNow.Day, $utcNow.Hour, $minute, 0)),
    [DateTimeKind]::Utc
  )
}

function Wait-ForCurrentM15Close {
  # Wait until the forming M15 UTC candle closes, then return the NEXT bar open
  # (that next bar is the one we skip for new entries after restart).
  $now = [DateTime]::UtcNow
  $currentOpen = Get-UtcM15Open $now
  $closeAt = $currentOpen.AddMinutes(15)
  $skipBar = $closeAt
  $waitSec = [Math]::Ceiling(($closeAt - [DateTime]::UtcNow).TotalSeconds) + 3
  if ($waitSec -lt 0) { $waitSec = 3 }
  if ($waitSec -gt 960) { $waitSec = 960 }
  Write-Log ("waiting for M15 close bar_open={0:yyyy-MM-ddTHH:mm:ss}Z skip_next={1:yyyy-MM-ddTHH:mm:ss}Z wait_sec={2}" -f $currentOpen, $skipBar, $waitSec)
  Start-Sleep -Seconds $waitSec
  return @{
    WaitedCloseBar = ($currentOpen.ToString("yyyy-MM-ddTHH:mm:ss") + "Z")
    SkipBarTime    = ($skipBar.ToString("yyyy-MM-ddTHH:mm:ss") + "Z")
  }
}

function Write-DeploySkipMarker {
  param(
    [string]$RootPath,
    [string]$SkipBarTime,
    [string]$WaitedCloseBar,
    [string]$Version
  )
  $stateDir = Join-Path $RootPath "state"
  New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
  $marker = Join-Path $stateDir "deploy_skip_bar.json"
  $payload = @{
    skip_bar_time       = $SkipBarTime
    waited_close_bar    = $WaitedCloseBar
    version             = $Version
    reason              = "auto_update_skip_next_bar"
    no_flatten          = $true
    preserve_positions  = $true
    preserve_sl         = $true
    written_at          = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  } | ConvertTo-Json -Depth 4
  Set-Content -Path $marker -Value $payload -Encoding UTF8
  Write-Log "wrote deploy skip marker $marker skip=$SkipBarTime (entries only; no flatten)"
}

function Copy-FromGitHubLayout {
  param([string]$GitDir, [string]$StagingDir)
  # Corp repo layout: Ali PC Deployment/runtime/*.py
  $runtime = Join-Path $GitDir "Ali PC Deployment\runtime"
  $scripts = Join-Path $GitDir "Ali PC Deployment\scripts"
  $mq5 = Join-Path $GitDir "Ali PC Deployment\mq5"
  if (Test-Path (Join-Path $runtime "bridge_trader.py")) {
    foreach ($name in $trackFiles) {
      $src = Join-Path $runtime $name
      if (Test-Path $src) { Copy-Item $src (Join-Path $StagingDir $name) -Force }
    }
    if (Test-Path $scripts) {
      Copy-Item $scripts (Join-Path $StagingDir "scripts") -Recurse -Force
    }
    if (Test-Path $mq5) {
      Copy-Item $mq5 (Join-Path $StagingDir "mq5") -Recurse -Force
    }
    return $true
  }
  # Flat layout: bridge_trader.py at git root
  if (Test-Path (Join-Path $GitDir "bridge_trader.py")) {
    foreach ($name in $trackFiles) {
      $src = Join-Path $GitDir $name
      if (Test-Path $src) { Copy-Item $src (Join-Path $StagingDir $name) -Force }
    }
    $scriptsSrc = Join-Path $GitDir "scripts"
    if (Test-Path $scriptsSrc) {
      Copy-Item $scriptsSrc (Join-Path $StagingDir "scripts") -Recurse -Force
    }
    return $true
  }
  return $false
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
    Write-Log "ERROR: safety_gate FAILED — refusing to copy live runtime"
    $resultPath = Join-Path $Root "state\last_update_result.json"
    New-Item -ItemType Directory -Force -Path (Split-Path $resultPath) | Out-Null
    @{
      ok = $false
      reason = "safety_gate_failed"
      checked_root = $CheckRoot
      utc = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json -Depth 4 | Set-Content $resultPath -Encoding UTF8
    return $false
  }
  Write-Log "safety_gate PASS"
  return $true
}

Write-Log "auto_update start profile=$Profile root=$Root force=$Force skipWait=$SkipCandleWait"

$updateUrl = Read-EnvValue "BRIDGE_UPDATE_URL"
$gitDir = Read-EnvValue "BRIDGE_UPDATE_GIT"
$gitRemote = Read-EnvValue "BRIDGE_UPDATE_GIT_REMOTE"
# Prefer git path when both are set (Latest is source of truth).
if (-not $gitDir) {
  $gitDir = "C:\onyxion-src\Latest_Gold_Trading_Bot"
}
$staging = Join-Path $env:TEMP ("onyxion-update-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Force -Path $staging | Out-Null

try {
  if ($gitDir -and (Test-Path (Join-Path $gitDir ".git"))) {
    Push-Location $gitDir
    if ($gitRemote) { git remote set-url origin $gitRemote 2>$null }
    elseif (-not $gitRemote) {
      $gitRemote = "https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git"
      git remote set-url origin $gitRemote 2>$null
    }
    git pull --ff-only 2>&1 | ForEach-Object { Write-Log "git: $_" }
    Pop-Location
    if (-not (Copy-FromGitHubLayout -GitDir $gitDir -StagingDir $staging)) {
      Write-Log "ERROR: git dir has no bridge_trader.py (flat or Ali PC Deployment/runtime)"
      exit 1
    }
  } elseif ($updateUrl) {
    $zipPath = Join-Path $env:TEMP "onyxion-bridge-bundle.zip"
    Write-Log "download $updateUrl"
    Invoke-LabWebRequest -Uri $updateUrl -OutFile $zipPath
    $zipHash = (Get-FileHash $zipPath -Algorithm SHA256).Hash
    Expand-Archive -Path $zipPath -DestinationPath $staging -Force
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
    $bundleVersion = "unknown"
    $verStaging = Join-Path $staging "VERSION.json"
    if (-not (Test-Path $verStaging)) {
      $br = Find-BundleRoot $staging
      if ($br) { $verStaging = Join-Path $br "VERSION.json" }
    }
    if (Test-Path $verStaging) {
      try {
        $vo = Get-Content $verStaging -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($vo.version) { $bundleVersion = [string]$vo.version }
      } catch { }
    }
    Write-Changelog -Action "RECEIVED" -Version $bundleVersion -Detail ("- source: ``{0}``" -f $updateUrl)
    Write-Changelog -Action "RECEIVED_HASH" -Version $bundleVersion -Detail ("- zip_sha256: ``{0}``" -f $zipHash)
  } else {
    Write-Log "SKIP: set BRIDGE_UPDATE_URL or BRIDGE_UPDATE_GIT in $Root\.env"
    exit 0
  }

  $bundleRoot = Find-BundleRoot $staging
  if (-not $bundleRoot) {
    Write-Log "ERROR: bundle missing bridge_trader.py"
    exit 1
  }

  $newFp = Get-BundleFingerprint $bundleRoot
  $oldFp = Get-BundleFingerprint $Root
  $incomingVersion = "unknown"
  $verIncoming = Join-Path $bundleRoot "VERSION.json"
  if (Test-Path $verIncoming) {
    try {
      $vo = Get-Content $verIncoming -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($vo.version) { $incomingVersion = [string]$vo.version }
    } catch { }
  }

  if (-not $Force -and $newFp -and $oldFp -and $newFp -eq $oldFp) {
    Write-Log "no change (fingerprint match)"
    Write-Changelog -Action "NO_CHANGE" -Version $incomingVersion -Detail "- fingerprint unchanged; local files already match bundle"
    exit 0
  }

  if (-not (Invoke-SafetyGate -CheckRoot $bundleRoot)) {
    Write-Changelog -Action "BLOCKED_SAFETY_GATE" -Version $incomingVersion -Detail "- refused apply; live runtime unchanged"
    exit 2
  }

  Write-Log "update detected - candle-safe apply (preserve state/ and open positions; never flatten)"
  foreach ($name in $trackFiles) {
    $src = Join-Path $Root $name
    if (Test-Path $src) { Copy-Item $src (Join-Path $backupDir $name) -Force }
  }

  $version = $incomingVersion
  if ($SkipCandleWait) {
    $skipPath = Join-Path $Root "state\deploy_skip_bar.json"
    $timing = @{
      WaitedCloseBar = ""
      SkipBarTime    = ""
    }
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
    Write-DeploySkipMarker -RootPath $Root -SkipBarTime $timing.SkipBarTime -WaitedCloseBar $timing.WaitedCloseBar -Version $version
  }

  $restart = $false
  $copiedFiles = @()
  foreach ($name in $trackFiles) {
    $from = Join-Path $bundleRoot $name
    if (-not (Test-Path $from)) { continue }
    $to = Join-Path $Root $name
    if ((Test-Path $to) -and $name -match "bridge_trader|mt5_live_engine|watchdog|connection_monitor|github_update_agent") {
      $oldH = (Get-FileHash $to -Algorithm SHA256).Hash
      $newH = (Get-FileHash $from -Algorithm SHA256).Hash
      if ($oldH -ne $newH) { $restart = $true }
    }
    Copy-Item $from $to -Force
    $copiedFiles += $name
    Write-Log "copied $name"
  }

  $scriptsFrom = Join-Path $bundleRoot "scripts"
  if (Test-Path $scriptsFrom) {
    Copy-Item "$scriptsFrom\*" (Join-Path $Root "scripts") -Recurse -Force
    $copiedFiles += "scripts/*"
    Write-Log "synced scripts/"
    $restart = $true
  }

  $mq5From = Join-Path $bundleRoot "mq5"
  if (Test-Path $mq5From) {
    Copy-Item "$mq5From\*" (Join-Path $Root "mq5") -Recurse -Force
    $copiedFiles += "mq5/*"
    Write-Log "synced mq5/ (recompile in MetaEditor if indicators changed)"
  }

  # NEVER touch .env or state/ (engine memory, open tickets, SL levels).
  Write-Log "preserved $Root\state and $Root\.env (no flatten on update)"

  if ($restart) {
    Write-Log "restarting bridge stack (positions stay open on MT5)"
    Stop-WatchdogForRoot $Root
    Start-WatchdogForProfile $Profile $Root
  }

  $restartFlag = if ($restart) { "yes" } else { "no" }
  Write-Log "update complete restart=$restartFlag waited_close=$($timing.WaitedCloseBar) skip_bar=$($timing.SkipBarTime)"
  Write-History "version=$version restart=$restartFlag waited_close=$($timing.WaitedCloseBar) skip_bar=$($timing.SkipBarTime) fingerprint=$newFp files=$($copiedFiles -join ', ')"
  $resultPath = Join-Path $Root "state\last_update_result.json"
  New-Item -ItemType Directory -Force -Path (Split-Path $resultPath) | Out-Null
  @{
    ok = $true
    version = $version
    restart = $restart
    waited_close_bar = $timing.WaitedCloseBar
    skip_bar_time = $timing.SkipBarTime
    fingerprint = $newFp
    files = $copiedFiles
    utc = (Get-Date).ToUniversalTime().ToString("o")
  } | ConvertTo-Json -Depth 5 | Set-Content $resultPath -Encoding UTF8
  $detailLines = @(
    "- files updated: $($copiedFiles -join ', ')",
    "- bridge restart: **$restartFlag**",
    "- waited for close of: ``$($timing.WaitedCloseBar)``",
    "- skipped entry candle: ``$($timing.SkipBarTime)``",
    "- open positions / SL / engine state: **preserved** (no flatten)",
    "- new fingerprint: ``$newFp``"
  )
  if ($Force) { $detailLines += "- note: forced update (-Force)" }
  Write-Changelog -Action "APPLIED_CANDLE_SAFE" -Version $version -Detail ($detailLines -join "`n")
} catch {
  Write-Log "ERROR: $($_.Exception.Message)"
  Write-Changelog -Action "ERROR" -Version "unknown" -Detail ("- error: ``{0}``" -f $_.Exception.Message)
  exit 1
} finally {
  if (Test-Path $staging) { Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue }
}
