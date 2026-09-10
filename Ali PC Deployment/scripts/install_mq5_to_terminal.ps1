#Requires -Version 5.1
<#
.SYNOPSIS
  Copy Onyxion MQ5 sources into the MT5 terminal and attempt MetaEditor compile.

.PARAMETER Profile
  hamza or ali — selects default install root C:\onyxion-{profile}

.PARAMETER Root
  Bridge install root (default C:\onyxion-{profile})
#>
[CmdletBinding()]
param(
  [ValidateSet("hamza", "ali")]
  [string]$Profile = "ali",
  [string]$Root = ""
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = "C:\onyxion-$Profile" }

$logDir = Join-Path $Root "logs"
$logFile = Join-Path $logDir "mq5_install.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-InstallLog($msg) {
  $line = "{0} {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss UTC"), $msg
  Add-Content -Path $logFile -Value $line -Encoding UTF8
  Write-Host $line
}

function Read-EnvValue($name) {
  $envPath = Join-Path $Root ".env"
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

function Find-MetaEditor {
  $candidates = @(
    "C:\Program Files\MetaTrader 5\metaeditor64.exe",
    "C:\Program Files (x86)\MetaTrader 5\metaeditor64.exe"
  )
  $terminalPath = Read-EnvValue "ASIM_MT5_TERMINAL_PATH"
  if (-not $terminalPath) {
    $terminalPath = Join-Path $Root "exness-mt5\terminal64.exe"
  }
  if ($terminalPath -and (Test-Path $terminalPath)) {
    $termRoot = Split-Path -Parent $terminalPath
    $candidates = @(
      (Join-Path $termRoot "metaeditor64.exe")
    ) + $candidates
  }
  foreach ($p in $candidates) {
    if ($p -and (Test-Path $p)) { return $p }
  }
  return $null
}

Write-InstallLog "mq5_install start profile=$Profile root=$Root"

$mq5Src = Join-Path $Root "mq5"
if (-not (Test-Path $mq5Src)) {
  Write-InstallLog "ERROR: mq5 source folder missing: $mq5Src"
  exit 1
}

$terminalExe = Read-EnvValue "ASIM_MT5_TERMINAL_PATH"
if (-not $terminalExe) {
  $terminalExe = Join-Path $Root "exness-mt5\terminal64.exe"
  Write-InstallLog "ASIM_MT5_TERMINAL_PATH not set — default $terminalExe"
}
if (-not (Test-Path $terminalExe)) {
  Write-InstallLog "ERROR: terminal not found: $terminalExe"
  exit 1
}

$termRoot = Split-Path -Parent $terminalExe
$indicatorsDir = Join-Path $termRoot "MQL5\Indicators"
$expertsDir = Join-Path $termRoot "MQL5\Experts"
New-Item -ItemType Directory -Force -Path $indicatorsDir, $expertsDir | Out-Null

$indicatorFiles = @("OnyxionHistogram.mq5", "OnyxionXTrendProxy.mq5")
$expertFiles = @(
  "OnyxionLabBridge.mq5",
  "OnyxionDemoBacktest.mq5",
  "OnyxionMT5ValuePublisher.mq5"
)

$copied = @()
foreach ($name in $indicatorFiles) {
  $src = Join-Path $mq5Src $name
  if (-not (Test-Path $src)) {
    Write-InstallLog "WARN: missing source $name"
    continue
  }
  $dest = Join-Path $indicatorsDir $name
  Copy-Item $src $dest -Force
  $copied += "Indicators\$name"
  Write-InstallLog "copied $name -> $dest"
}

foreach ($name in $expertFiles) {
  $src = Join-Path $mq5Src $name
  if (-not (Test-Path $src)) {
    Write-InstallLog "WARN: missing source $name"
    continue
  }
  $dest = Join-Path $expertsDir $name
  Copy-Item $src $dest -Force
  $copied += "Experts\$name"
  Write-InstallLog "copied $name -> $dest"
}

$metaEditor = Find-MetaEditor
if ($metaEditor) {
  Write-InstallLog "MetaEditor: $metaEditor"
  foreach ($rel in $copied) {
    $target = Join-Path $termRoot ("MQL5\" + $rel)
    $logPath = Join-Path $logDir ("compile_" + [IO.Path]::GetFileNameWithoutExtension($target) + ".log")
    Write-InstallLog "compile $target"
    & $metaEditor "/compile:$target" "/log:$logPath" 2>&1 | ForEach-Object { Write-InstallLog "metaeditor: $_" }
    if (Test-Path $logPath) {
      Get-Content $logPath -Tail 5 -ErrorAction SilentlyContinue | ForEach-Object {
        Write-InstallLog "  $_"
      }
    }
  }
} else {
  Write-InstallLog "MetaEditor not found — copy complete; compile manually in MetaEditor (F7)"
}

Write-InstallLog "mq5_install complete copied=$($copied.Count) files"
Write-Host ""
Write-Host "Log: $logFile" -ForegroundColor Cyan
