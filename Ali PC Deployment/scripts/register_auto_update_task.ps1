#Requires -Version 5.1
<#
  Register Windows Scheduled Task: run auto_update_bridge.ps1 every N minutes.
  Default profile=ali for friend's laptop.
#>
[CmdletBinding()]
param(
  [ValidateSet("hamza", "ali")]
  [string]$Profile = "ali",
  [int]$IntervalMinutes = 15,
  [string]$Root = ""
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = "C:\onyxion-$Profile" }
$taskName = "OnyxionBridgeAutoUpdate-$Profile"

$scriptHere = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPath = Join-Path $Root "scripts\auto_update_bridge.ps1"
if (-not (Test-Path $scriptPath)) {
  $scriptPath = Join-Path $scriptHere "auto_update_bridge.ps1"
  if (-not (Test-Path $scriptPath)) { throw "auto_update_bridge.ps1 not found" }
}

$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -Profile $Profile -Root `"$Root`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory $Root

# Repeat every 15 min for 10 years (avoids MaxValue issues on some Windows builds)
$startAt = (Get-Date).AddMinutes(1)
$triggerRepeat = New-ScheduledTaskTrigger -Once -At $startAt `
  -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
  -RepetitionDuration (New-TimeSpan -Days 3650)

$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -DontStopOnIdleEnd `
  -AllowStartIfOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($triggerRepeat, $triggerLogon) `
  -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Registered $taskName" -ForegroundColor Green
Write-Host "  Every $IntervalMinutes minutes + at logon"
Write-Host "  Log: $Root\logs\auto_update.log"
Write-Host ""
Write-Host "Test now:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "  Get-Content '$Root\logs\auto_update.log' -Tail 15 -Wait"
