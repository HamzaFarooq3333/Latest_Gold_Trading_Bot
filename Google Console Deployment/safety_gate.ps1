# Safety / compile gate before copying runtime or redeploying desk.
# Usage: .\safety_gate.ps1 -Root "C:\onyxion-ali"   OR  -Root "<repo root>"
param(
  [Parameter(Mandatory = $false)]
  [string]$Root = "",
  [switch]$NoDashboard,
  [switch]$Json
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @(
  (Join-Path $ScriptDir "app\safety_gate_check.py"),
  (Join-Path $ScriptDir "safety_gate_check.py"),
  (Join-Path (Split-Path -Parent $ScriptDir) "Ali PC Deployment\runtime\safety_gate_check.py"),
  (Join-Path (Split-Path -Parent $ScriptDir) "Ali PC Deployment\scripts\safety_gate_check.py")
)
$Checker = $null
foreach ($c in $candidates) {
  if (Test-Path $c) { $Checker = $c; break }
}
if (-not $Checker) {
  Write-Error "safety_gate_check.py not found near $ScriptDir"
  exit 1
}

if (-not $Root) {
  $Root = Split-Path -Parent $ScriptDir
}

$py = $null
foreach ($c in @("python", "py")) {
  $cmd = Get-Command $c -ErrorAction SilentlyContinue
  if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) {
  Write-Error "python not on PATH"
  exit 1
}

$argList = @($Checker, "--root", $Root)
if ($NoDashboard) { $argList += "--no-dashboard" }
if ($Json) { $argList += "--json" }

& $py @argList
exit $LASTEXITCODE
