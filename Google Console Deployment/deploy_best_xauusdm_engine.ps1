# Deploy XAUUSDm-winning engine to both Linux desks + both Windows MT5 bridges.
$ErrorActionPreference = "Stop"
$Gcloud = "D:\Google Cloud\google-cloud-sdk\bin\gcloud.ps1"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $Root "app"
$Zone = "us-central1-a"
$Project = "agent-gold-507217"
$Ctrl = '{"VOLUME":0.01,"TSL_PTS":0.25,"BROKER_MIN_STOP_PTS":0.30,"TRAIL_EVERY_CANDLE":1,"ENTRY_BAR_MODE":"defer","STOP_SLIPPAGE_PTS":0.25,"DISABLE_STOP_LOSS":0,"XTREND_GATE":1}'

if (-not (Test-Path $Gcloud)) { throw "gcloud not found at $Gcloud" }

$desks = @(
  @{ Name = "hamzatestserver01"; EnvFile = "env.hamzatestserver01"; User = "hamza"; Pass = "123451"; Url = "https://35.232.76.12" },
  @{ Name = "instance-20260831-171822"; EnvFile = "env.instance-20260831-171822"; User = "ali"; Pass = "123451"; Url = "https://35.223.235.204" }
)

foreach ($d in $desks) {
  Write-Host "=== Linux desk $($d.Name) ===" -ForegroundColor Cyan
  & $Gcloud compute scp (Join-Path $App "mt5_live_engine.py") "$($d.Name):/tmp/mt5_live_engine.py" --zone=$Zone --project=$Project
  & $Gcloud compute scp (Join-Path $App "server.py") "$($d.Name):/tmp/server.py" --zone=$Zone --project=$Project
  & $Gcloud compute scp (Join-Path $App "broker_live.py") "$($d.Name):/tmp/broker_live.py" --zone=$Zone --project=$Project
  & $Gcloud compute scp (Join-Path $Root $d.EnvFile) "$($d.Name):/tmp/asim.env" --zone=$Zone --project=$Project
  & $Gcloud compute scp (Join-Path $Root "remote_install_best_engine.sh") "$($d.Name):/tmp/remote_install_best_engine.sh" --zone=$Zone --project=$Project
  & $Gcloud compute ssh $d.Name --zone=$Zone --project=$Project --command="sed -i 's/\r$//' /tmp/remote_install_best_engine.sh && bash /tmp/remote_install_best_engine.sh"
}

foreach ($d in $desks) {
  Write-Host "=== Live controls $($d.Name) ===" -ForegroundColor Cyan
  $pair = $d.User + ":" + $d.Pass
  curl.exe -k -s -u $pair -H "Content-Type: application/json" -d $Ctrl "$($d.Url)/api/broker/controls"
  Write-Host ""
}

$win = @(
  @{ Name = "hamza-mt5-windows"; Root = "C:\onyxion-hamza"; Task = "OnyxionBridgeStack-hamza" },
  @{ Name = "ali-mt5-windows"; Root = "C:\onyxion-ali"; Task = "OnyxionBridgeStack-ali" }
)
foreach ($w in $win) {
  Write-Host "=== Windows bridge $($w.Name) ===" -ForegroundColor Cyan
  & $Gcloud compute scp (Join-Path $App "mt5_live_engine.py") "$($w.Name):mt5_live_engine.py" --zone=$Zone --project=$Project
  $root = $w.Root
  $task = $w.Task
  $cmd = @"
`$root = '$root'
`$src = Get-ChildItem -Path `$env:USERPROFILE,`$PWD,C:\tmp,C:\Windows\Temp -Filter mt5_live_engine.py -Recurse -Depth 2 -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not `$src) { throw 'uploaded mt5_live_engine.py not found' }
New-Item -ItemType Directory -Force -Path `$root | Out-Null
Copy-Item -Force `$src.FullName (Join-Path `$root 'mt5_live_engine.py')
Write-Host ('engine from ' + `$src.FullName)
`$envPath = Join-Path `$root '.env'
if (Test-Path `$envPath) {
  `$txt = Get-Content `$envPath -Raw
  foreach (`$pair in @(
    @('TSL_PTS','0.25'),
    @('BROKER_MIN_STOP_PTS','0.30'),
    @('TRAIL_EVERY_CANDLE','1'),
    @('ENTRY_BAR_MODE','defer'),
    @('DISABLE_STOP_LOSS','0'),
    @('SKIP_WEEKENDS','1')
  )) {
    `$k = `$pair[0]; `$v = `$pair[1]
    if (`$txt -match "(?m)^`$k=") { `$txt = [regex]::Replace(`$txt, "(?m)^`$k=.*$", "`$k=`$v") }
    else { `$txt = `$txt.TrimEnd() + "`r`n`$k=`$v`r`n" }
  }
  `$utf8 = New-Object System.Text.UTF8Encoding `$false
  [IO.File]::WriteAllText(`$envPath, `$txt, `$utf8)
  if (Test-Path (Join-Path `$root '.env.txt')) { Copy-Item `$envPath (Join-Path `$root '.env.txt') -Force }
  Write-Host 'env patched'
}
Select-String -Path (Join-Path `$root 'mt5_live_engine.py') -Pattern 'stop_fill = slipped_stop' | Select-Object -First 1 | ForEach-Object { Write-Host `$_.Line.Trim() }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { `$_.CommandLine -like '*watchdog*' -or `$_.CommandLine -like '*bridge_trader*' } |
  ForEach-Object { Stop-Process -Id `$_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2
try { Start-ScheduledTask -TaskName '$task' } catch { Write-Host ('task start warn: ' + `$_.Exception.Message) }
Start-Sleep 6
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { `$_.CommandLine -like '*watchdog*' -or `$_.CommandLine -like '*bridge*' } |
  ForEach-Object { Write-Host ('python pid=' + `$_.ProcessId) }
"@
  & $Gcloud compute ssh $w.Name --zone=$Zone --project=$Project --command=$cmd
}

Write-Host "=== DONE ===" -ForegroundColor Green
