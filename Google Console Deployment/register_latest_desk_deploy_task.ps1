#Requires -Version 5.1
<#
  Register task OnyxionLatest-DeskDeployWatch
  Auto-redeploys GCP desk when Hamza OR Ali pushes to Latest.
#>
[CmdletBinding()]
param(
  [string]$Workspace = "d:\Company\Onyxion\Week 7\New folder\New folder\New folder"
)

$ErrorActionPreference = "Stop"
$taskName = "OnyxionLatest-DeskDeployWatch"
$script = Join-Path $Workspace "Google Console Deployment\watch_latest_desk_deploy.ps1"
if (-not (Test-Path $script)) { throw "Missing $script" }

$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Workspace `"$Workspace`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory (Split-Path $script)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -DontStopOnIdleEnd `
  -AllowStartIfOnBatteries `
  -RestartCount 5 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

try {
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
  Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  Write-Host "Registered + started $taskName" -ForegroundColor Green
} catch {
  Write-Host "Register-ScheduledTask failed ($($_.Exception.Message)). Starting watcher in background..." -ForegroundColor Yellow
  Start-Process -FilePath "powershell.exe" -ArgumentList $arg -WindowStyle Hidden
}

Write-Host "  Watches: HamzaFarooq3333/Latest_Gold_Trading_Bot (Hamza OR Ali)"
Write-Host "  Action:  safety_gate -> apply local -> GCP desk redeploy"
Write-Host "  Log:     $Workspace\Google Console Deployment\logs\latest_desk_deploy.log"
Write-Host "  Status:  $Workspace\Google Console Deployment\logs\latest_desk_deploy_status.json"
