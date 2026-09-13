#Requires -Version 5.1
<#
  Register the four Ali PC scheduled tasks (user-level, hidden, no console):

    OnyxionAli-MT5                launch_mt5.ps1        MT5 terminal + auto-login
    OnyxionAli-BridgeStack        start_bridge_stack     watchdog -> bridge_trader.py
    OnyxionAli-ConnectionMonitor  connection_monitor.py  5-min health checks / repairs
    OnyxionAli-GitHubAgent        github_update_agent.py candle-safe GitOps + desk status

  Each runs at logon and re-fires every 5 minutes; every script is a no-op
  when its process is already up (file locks / process checks), so the
  repetition only ever restarts something that died.

  The old 15-minute zip updater task (OnyxionBridgeAutoUpdate-*) is removed:
  the GitHub agent is the only updater now.
#>
[CmdletBinding()]
param(
  [string]$Root = "C:\onyxion-ali"
)

$ErrorActionPreference = "Stop"
$pyw = Join-Path $Root "venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { throw "venv missing: $pyw (run INSTALL.ps1 first)" }
$user = $env:USERNAME

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -AllowStartIfOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden -MultipleInstances IgnoreNew
# Limited, not Highest: Highest needs an elevated console to register and the
# tasks need no admin rights.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$triggers = @(
  (New-ScheduledTaskTrigger -AtLogOn -User $user),
  (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650))
)

function Register-Hidden([string]$name, [string]$exe, [string]$argument) {
  $action = New-ScheduledTaskAction -Execute $exe -Argument $argument -WorkingDirectory $Root
  Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Out-Null
  Write-Host "registered $name" -ForegroundColor Green
}

$ps = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden"
Register-Hidden "OnyxionAli-MT5" "powershell.exe" "$ps -File `"$Root\scripts\launch_mt5.ps1`" -Root `"$Root`""
Register-Hidden "OnyxionAli-BridgeStack" "powershell.exe" "$ps -Command `"& '$Root\fix_script_encoding.ps1' -Root '$Root'; & '$Root\scripts\start_bridge_stack.ps1' -Profile ali -Root '$Root'`""
Register-Hidden "OnyxionAli-ConnectionMonitor" $pyw "`"$Root\connection_monitor.py`""
Register-Hidden "OnyxionAli-GitHubAgent" $pyw "`"$Root\github_update_agent.py`""

foreach ($legacy in @("OnyxionBridgeAutoUpdate-ali", "OnyxionBridgeAutoUpdate-hamza", "OnyxionBridgeStack-ali")) {
  if (Get-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue) {
    try {
      Unregister-ScheduledTask -TaskName $legacy -Confirm:$false
      Write-Host "removed legacy task $legacy" -ForegroundColor Yellow
    } catch {
      Disable-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue | Out-Null
      Write-Host "disabled legacy task $legacy (owned by another account; remove it from Task Scheduler)" -ForegroundColor Yellow
    }
  }
}
Write-Host "Start now:  Get-ScheduledTask 'OnyxionAli-*' | Start-ScheduledTask"
