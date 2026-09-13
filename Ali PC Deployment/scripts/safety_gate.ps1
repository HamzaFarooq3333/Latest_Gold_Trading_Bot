#Requires -Version 5.1
<#
  Safety / compile gate before copying runtime or redeploying the desk.
  Usage: safety_gate.ps1 -Root "C:\onyxion-ali"   OR   -Root "<repo root>"
  Exit code is the checker's: 0 = pass, 1 = refuse.
#>
param(
  [string]$Root = "",
  [switch]$NoDashboard,
  [switch]$Json
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @(
  (Join-Path (Split-Path -Parent $ScriptDir) "runtime\safety_gate_check.py"),   # repo pack layout
  (Join-Path (Split-Path -Parent $ScriptDir) "safety_gate_check.py")            # flat install root
)
$Checker = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Checker) {
  Write-Error "safety_gate_check.py not found near $ScriptDir"
  exit 1
}
if (-not $Root) { $Root = Split-Path -Parent $ScriptDir }

# Prefer the install's venv (it has every dependency the runtime imports).
$py = $null
foreach ($c in @((Join-Path $Root "venv\Scripts\python.exe"), (Join-Path (Split-Path -Parent $ScriptDir) "venv\Scripts\python.exe"))) {
  if (Test-Path $c) { $py = $c; break }
}
if (-not $py) {
  foreach ($c in @("python", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
  }
}
if (-not $py) {
  Write-Error "python not found"
  exit 1
}

$argList = @($Checker, "--root", $Root)
if ($NoDashboard) { $argList += "--no-dashboard" }
if ($Json) { $argList += "--json" }
& $py @argList
exit $LASTEXITCODE
