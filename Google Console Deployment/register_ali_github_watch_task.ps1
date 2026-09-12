#Requires -Version 5.1
<#
  Register Windows Scheduled Task: OnyxionAli-GitHubToGcpWatch
  Starts at logon and keeps watching Latest for Ali pushes → auto GCP desk redeploy.
#>
[CmdletBinding()]
param(
  [string]$Workspace = "d:\Company\Onyxion\Week 7\New folder\New folder\New folder"
)

$ErrorActionPreference = "Stop"
$taskName = "OnyxionAli-GitHubToGcpWatch"
$script = Join-Path $Workspace "Google Console Deployment\watch_ali_github.ps1"
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
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force | Out-Null

Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

Write-Host "Registered + started $taskName" -ForegroundColor Green
Write-Host "  Watches: HamzaFarooq3333/Latest_Gold_Trading_Bot (Ali pushes)"
Write-Host "  Action:  safety_gate → apply local → GCP desk redeploy"
Write-Host "  Log:     $Workspace\Google Console Deployment\logs\ali_github_watch.log"
Write-Host "  Status:  $Workspace\Google Console Deployment\logs\ali_push_deploy_status.json"
Write-Host ""
Write-Host "When you come back online, clone Latest:"
Write-Host "  git clone https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git"
