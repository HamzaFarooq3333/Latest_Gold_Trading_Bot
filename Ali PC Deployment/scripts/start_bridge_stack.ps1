#Requires -Version 5.1
[CmdletBinding()]
param(
  [ValidateSet("hamza", "ali")]
  [string]$Profile = "ali",
  [string]$Root = "",
  # Empty by default so the venv under $Root wins. Hard-coding a global
  # interpreter here is what caused duplicate stacks: auto_update_bridge.ps1,
  # connection_monitor.py and github_update_agent.py all call this script
  # WITHOUT -PythonPath, so they started a second watchdog+bridge under
  # C:\Program Files\Python312 while the scheduled task ran the venv one.
  # Two bridges on one account means duplicate orders.
  [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = "C:\onyxion-$Profile" }
$watchdog = Join-Path $Root "watchdog_exness.py"
if (-not (Test-Path $watchdog)) { throw "watchdog not found: $watchdog — run install_bridge.ps1 first" }
if (-not $PythonPath) {
  $venvPy = Join-Path $Root "venv\Scripts\python.exe"
  $PythonPath = if (Test-Path $venvPy) { $venvPy } else { "C:\Program Files\Python312\python.exe" }
}
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
