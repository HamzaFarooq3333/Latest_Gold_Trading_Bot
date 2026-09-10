#Requires -Version 5.1
<#
.SYNOPSIS
  Verify the bridge auto-update pipeline on an MT5 laptop.

.PARAMETER Profile
  hamza or ali

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

$taskName = "OnyxionBridgeAutoUpdate-$Profile"
$results = @()

function Add-Check($name, [bool]$pass, [string]$detail) {
  $script:results += [PSCustomObject]@{
    Check = $name
    Result = if ($pass) { "PASS" } else { "FAIL" }
    Detail = $detail
  }
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

function Test-LabInsecureTls {
  $v = Read-EnvValue "ASIM_LAB_INSECURE"
  if ($null -eq $v -or [string]::IsNullOrWhiteSpace($v)) { return $true }
  return $v.Trim().ToLower() -notin @("0", "false", "no")
}

Write-Host "=== Onyxion auto-update verify ($Profile) ===" -ForegroundColor Cyan
Write-Host "Root: $Root`n"

# 1) Scheduled task
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Add-Check "Scheduled task" ($null -ne $task) $(if ($task) { "Registered: $taskName" } else { "Missing: $taskName" })

# 2) BRIDGE_UPDATE_URL
$updateUrl = Read-EnvValue "BRIDGE_UPDATE_URL"
Add-Check "BRIDGE_UPDATE_URL" ([bool]$updateUrl) $(if ($updateUrl) { $updateUrl } else { "Not set in $Root\.env" })

# 3) Test download (TLS bypass for GCP self-signed bundle host)
$downloadOk = $false
$downloadDetail = "Skipped — no URL"
if ($updateUrl) {
  $tmpZip = Join-Path $env:TEMP ("onyxion-verify-" + [guid]::NewGuid().ToString("n") + ".zip")
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $usedCurl = $false
    if (Test-LabInsecureTls) {
      $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
      if ($curl) {
        & curl.exe -k -s -L --connect-timeout 60 --max-time 300 -o $tmpZip $updateUrl
        $usedCurl = $true
        if ($LASTEXITCODE -ne 0) { throw "curl exit $LASTEXITCODE" }
      } else {
        $prev = [System.Net.ServicePointManager]::ServerCertificateValidationCallback
        [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
        try {
          Invoke-WebRequest -Uri $updateUrl -OutFile $tmpZip -UseBasicParsing -TimeoutSec 60
        } finally {
          [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $prev
        }
      }
    } else {
      Invoke-WebRequest -Uri $updateUrl -OutFile $tmpZip -UseBasicParsing -TimeoutSec 60
    }
    $size = (Get-Item $tmpZip).Length
    $downloadOk = $size -gt 1000
    $via = if ($usedCurl) { "curl.exe" } else { "Invoke-WebRequest" }
    $downloadDetail = "Downloaded $size bytes via $via"
  } catch {
    $downloadDetail = $_.Exception.Message
  } finally {
    if (Test-Path $tmpZip) { Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue }
  }
}
Add-Check "Bundle download" $downloadOk $downloadDetail

# 4) Logs readable
$logDir = Join-Path $Root "logs"
$autoLog = Join-Path $logDir "auto_update.log"
$historyLog = Join-Path $logDir "update_history.log"
$changelogFile = Join-Path $Root "CHANGELOG.md"
$logDirOk = (Test-Path $logDir) -and (Test-Path $logDir -PathType Container)
Add-Check "logs directory" $logDirOk $(if ($logDirOk) { "Readable: $logDir" } else { "Missing: $logDir" })
if ($logDirOk) {
  $autoNote = if (Test-Path $autoLog) { "exists" } else { "not created yet (normal before first run)" }
  $histNote = if (Test-Path $historyLog) { "exists" } else { "not created yet (normal before first update)" }
  Add-Check "auto_update.log" $true "auto_update.log $autoNote"
  Add-Check "update_history.log" $true "update_history.log $histNote"
  $chgNote = if (Test-Path $changelogFile) { "exists" } else { "created on first auto-update run" }
  Add-Check "CHANGELOG.md" $true "CHANGELOG.md $chgNote"
}

# Summary
Write-Host ""
$results | Format-Table -AutoSize
$failCount = @($results | Where-Object { $_.Result -eq "FAIL" }).Count
$passCount = @($results | Where-Object { $_.Result -eq "PASS" }).Count

Write-Host ""
if ($failCount -eq 0) {
  Write-Host "SUMMARY: PASS ($passCount checks)" -ForegroundColor Green
  exit 0
} else {
  Write-Host "SUMMARY: FAIL ($failCount failed, $passCount passed)" -ForegroundColor Red
  exit 1
}
