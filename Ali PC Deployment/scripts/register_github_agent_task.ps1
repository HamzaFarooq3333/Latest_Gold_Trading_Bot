#Requires -Version 5.1
<#
  Register long-running GitHub update agent for Ali PC.
  Task name: OnyxionAli-GitHubAgent
#>
[CmdletBinding()]
param(
  [string]$Root = "C:\onyxion-ali"
)

$ErrorActionPreference = "Stop"
$taskName = "OnyxionAli-GitHubAgent"
$pyw = Join-Path $Root "venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) {
  $pyw = Join-Path $Root "venv\Scripts\python.exe"
}
$agent = Join-Path $Root "github_update_agent.py"
if (-not (Test-Path $agent)) {
  throw "github_update_agent.py not found at $agent — copy runtime pack first"
}

$arg = "`"$agent`""
$action = New-ScheduledTaskAction -Execute $pyw -Argument $arg -WorkingDirectory $Root
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -DontStopOnIdleEnd `
  -AllowStartIfOnBatteries `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggerLogon `
  -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Registered $taskName" -ForegroundColor Green
Write-Host "  Log: $Root\logs\github_update_agent.log"
Write-Host "  Start-ScheduledTask -TaskName '$taskName'"
