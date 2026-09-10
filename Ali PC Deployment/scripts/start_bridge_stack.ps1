#Requires -Version 5.1
[CmdletBinding()]
param(
  [ValidateSet("hamza", "ali")]
  [string]$Profile = "ali",
  [string]$Root = "",
  [string]$PythonPath = "C:\Program Files\Python312\python.exe"
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = "C:\onyxion-$Profile" }
$watchdog = Join-Path $Root "watchdog_exness.py"
if (-not (Test-Path $watchdog)) { throw "watchdog not found: $watchdog — run install_bridge.ps1 first" }
if (-not (Test-Path $PythonPath)) {
  $PythonPath = (Get-Command python.exe -ErrorAction Stop).Source
}

New-Item -ItemType Directory -Force -Path "$Root\logs", "$Root\state" | Out-Null

$watchdogName = Split-Path -Leaf $watchdog
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match [regex]::Escape($watchdogName) -and $_.CommandLine -like "*$Root*" } |
  Select-Object -First 1
if ($existing) {
  Write-Host "Watchdog already running pid=$($existing.ProcessId) root=$Root"
  exit 0
}

$env:MT5_BRIDGE_MODEL = "ASIM"
$env:MT5_ROOT = $Root
Start-Process -FilePath $PythonPath -ArgumentList "`"$watchdog`"" -WorkingDirectory $Root -WindowStyle Hidden
Write-Host "Started ASIM watchdog for $Profile at $Root" -ForegroundColor Green
