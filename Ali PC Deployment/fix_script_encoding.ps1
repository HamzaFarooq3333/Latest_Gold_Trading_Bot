#Requires -Version 5.1
<#
  Repair PowerShell script encoding after an auto-update sync.

  WHY THIS EXISTS
  The repo ships scripts\*.ps1 as UTF-8 WITHOUT a BOM, and several of them
  contain non-ASCII punctuation (em dashes) inside strings. Windows
  PowerShell 5.1 reads a BOM-less file using the system ANSI codepage, so
  each em dash becomes multiple bytes, string terminators are lost, and the
  file dies with "The string is missing the terminator" before a single line
  runs. start_bridge_stack.ps1 is one of the affected files, so every
  auto-update would otherwise sync working code and then fail to restart the
  bridge with it.

  Adding a UTF-8 BOM makes PowerShell 5.1 decode the file as UTF-8 and the
  scripts parse correctly. Content is never altered - only the encoding.

  This file deliberately lives in the install ROOT, not in scripts\, because
  auto_update_bridge.ps1 overwrites scripts\ wholesale on every sync.
#>
[CmdletBinding()]
param(
  [string]$Root = "C:\onyxion-ali"
)

$ErrorActionPreference = "Stop"
$scriptDir = Join-Path $Root "scripts"
if (-not (Test-Path $scriptDir)) { return }

$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "auto_update.log"

$enc = New-Object System.Text.UTF8Encoding($true)
$fixed = @()

foreach ($f in (Get-ChildItem $scriptDir -Filter *.ps1 -Recurse -ErrorAction SilentlyContinue)) {
  try {
    $bytes = [IO.File]::ReadAllBytes($f.FullName)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    if ($hasBom) { continue }

    # Only rewrite when the file actually parses as UTF-8 containing
    # non-ASCII bytes; pure-ASCII files are already unambiguous.
    $nonAscii = $false
    foreach ($b in $bytes) { if ($b -gt 127) { $nonAscii = $true; break } }
    if (-not $nonAscii) { continue }

    [IO.File]::WriteAllText($f.FullName, [Text.Encoding]::UTF8.GetString($bytes), $enc)
    $fixed += $f.Name
  } catch {
    Add-Content -Path $logFile -Encoding UTF8 -Value (
      "{0} encoding-repair ERROR {1}: {2}" -f `
        (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss"), $f.Name, $_.Exception.Message)
  }
}

if ($fixed.Count -gt 0) {
  $msg = "{0} encoding-repair added UTF-8 BOM to: {1}" -f `
    (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss"), ($fixed -join ", ")
  Add-Content -Path $logFile -Encoding UTF8 -Value $msg
  Write-Host $msg
} else {
  Write-Host "encoding-repair: nothing to fix"
}
