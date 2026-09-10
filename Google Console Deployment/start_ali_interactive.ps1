$ErrorActionPreference = "Stop"
$Root = "C:\onyxion-ali"
$py = "C:\Program Files\Python312\python.exe"
$TaskName = "OnyxionBridgeStack-ali"
$DeskUrl = "https://35.223.235.204"

if (-not (Test-Path "$Root\bridge_trader.py")) { throw "Missing bridge at $Root" }
if (-not (Test-Path "$Root\.env")) { throw "Missing .env" }
if (-not (Test-Path $py)) { $py = (Get-Command python.exe).Source }

# Fresh start: clear local state + wipe desk history before launching Python
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*watchdog*" -or $_.CommandLine -like "*bridge_trader*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path "$Root\logs","$Root\scripts","$Root\state" | Out-Null
Get-ChildItem "$Root\logs" -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem "$Root\state" -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
try {
  Add-Type @"
using System.Net;using System.Net.Security;using System.Security.Cryptography.X509Certificates;
public class TrustAllCerts { public static bool CB(object s, X509Certificate c, X509Chain ch, SslPolicyErrors e){ return true; } }
"@ -ErrorAction SilentlyContinue
  [System.Net.ServicePointManager]::ServerCertificateValidationCallback = [TrustAllCerts]::CB
  [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
  Invoke-RestMethod -Method POST -Uri "$DeskUrl/api/broker/reset" -ContentType "application/json" -Body "{}" -TimeoutSec 30 | Out-Null
  Write-Host "Desk wiped: $DeskUrl"
} catch { Write-Host "WARN desk wipe: $($_.Exception.Message)" }

$startScript = "$Root\scripts\start_bridge_stack.ps1"
@'
$env:MT5_BRIDGE_MODEL = "ASIM"
$env:MT5_ROOT = "C:\onyxion-ali"
Set-Location "C:\onyxion-ali"
$py = "C:\Program Files\Python312\python.exe"
if (-not (Test-Path $py)) { $py = (Get-Command python.exe).Source }
$log = "C:\onyxion-ali\logs\task_start.log"
Add-Content $log ("{0} task start" -f (Get-Date -Format o))
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*watchdog*" -or $_.CommandLine -like "*bridge_trader*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2
Start-Process -FilePath $py -ArgumentList "`"C:\onyxion-ali\watchdog_exness.py`"" -WorkingDirectory "C:\onyxion-ali" -WindowStyle Hidden
Add-Content $log ("{0} watchdog launched" -f (Get-Date -Format o))
'@ | Set-Content $startScript -Encoding UTF8

$user = $env:USERNAME
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -AllowStartIfOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Enable-ScheduledTask -TaskName $TaskName | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Started $TaskName"
Start-Sleep 10

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*watchdog*" -or $_.CommandLine -like "*bridge*" } |
  ForEach-Object { Write-Host "python pid=$($_.ProcessId) sess=$($_.SessionId)" }

if (Test-Path "$Root\logs\bridge_asim.log") {
  Write-Host "=== bridge log ==="
  Get-Content "$Root\logs\bridge_asim.log" -Tail 20
}
if (Test-Path "$Root\logs\exness_watchdog_asim.log") {
  Write-Host "=== watchdog log ==="
  Get-Content "$Root\logs\exness_watchdog_asim.log" -Tail 10
}
Write-Host "ALI_START_DONE"
