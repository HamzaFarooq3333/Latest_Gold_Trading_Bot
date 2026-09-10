#Requires -Version 5.1
<#
  Sync Ali PC Deployment\runtime from Google Console Deployment\app
  (keeps MT5 PC pack on the same engine logic as the live desk).

  Usage (from repo root or this folder):
    powershell -ExecutionPolicy Bypass -File ".\Ali PC Deployment\scripts\sync_runtime_from_gcp_app.ps1"
#>
[CmdletBinding()]
param(
  [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$packRoot = Split-Path -Parent $here
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $packRoot }

$srcApp = Join-Path $RepoRoot "Google Console Deployment\app"
$dstRuntime = Join-Path $packRoot "runtime"
$dstDocs = Join-Path $packRoot "docs"

$need = @(
  @{ Src = "mt5_live_engine.py"; Dst = "mt5_live_engine.py" },
  @{ Src = "bridge_trader.py"; Dst = "bridge_trader.py" }
)
foreach ($f in $need) {
  $from = Join-Path $srcApp $f.Src
  $to = Join-Path $dstRuntime $f.Dst
  if (-not (Test-Path $from)) { throw "Missing source: $from" }
  Copy-Item $from $to -Force
  Write-Host "Synced $($f.Src)" -ForegroundColor Green
}

$rules = Join-Path $RepoRoot "TRADING_RULES.md"
if (Test-Path $rules) {
  Copy-Item $rules (Join-Path $dstDocs "TRADING_RULES.md") -Force
  Write-Host "Synced TRADING_RULES.md" -ForegroundColor Green
}

$stamp = Get-Date -Format "yyyy-MM-dd-HHmmss"
$ver = @{
  version = "ali-pc-synced-$stamp"
  built_utc = (Get-Date).ToUniversalTime().ToString("o")
  engine = "Synced from Google Console Deployment/app"
  live_desk = "https://35.253.21.246"
  notes = "Primary XT clear; SUPP raw wick; TRAIL_ENTRY_BAR; XTREND_GATE_SUPP"
}
$ver | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $dstRuntime "VERSION.json") -Encoding UTF8
Write-Host "Wrote VERSION.json → $($ver.version)" -ForegroundColor Cyan
Write-Host "Done. Re-run INSTALL.ps1 on Ali PC (or copy runtime\ into C:\onyxion-ali) and force auto_update if using the GCP zip." -ForegroundColor Yellow
