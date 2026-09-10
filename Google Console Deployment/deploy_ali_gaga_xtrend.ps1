# Deploy KJ GagaTrend X-Trend to Ali GCP desk + Windows MT5 bridge.
$ErrorActionPreference = "Stop"
$Gcloud = "D:\Google Cloud\google-cloud-sdk\bin\gcloud.ps1"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $Root "app"
$Zone = "us-central1-a"
$Project = "agent-gold-507217"
$Desk = "instance-20260831-171822"
$Win = "ali-mt5-windows"
$WinRoot = "C:\onyxion-ali"
$WinTask = "OnyxionBridgeStack-ali"

if (-not (Test-Path $Gcloud)) { throw "gcloud not found at $Gcloud" }

Write-Host "=== Ali Linux desk: bridge + env ===" -ForegroundColor Cyan
& $Gcloud compute scp (Join-Path $App "bridge_trader.py") "${Desk}:/tmp/bridge_trader.py" --zone=$Zone --project=$Project
& $Gcloud compute scp (Join-Path $Root "env.instance-20260831-171822") "${Desk}:/tmp/asim.env" --zone=$Zone --project=$Project
& $Gcloud compute ssh $Desk --zone=$Zone --project=$Project --command@"
set -e
SECRET=`$(grep '^AUTH_SECRET=' /opt/asim-gcp/.env 2>/dev/null || true)
sudo cp /tmp/bridge_trader.py /opt/asim-gcp/bridge_trader.py
sudo cp /tmp/asim.env /opt/asim-gcp/.env
if [ -n "`$SECRET" ] && ! grep -q '^AUTH_SECRET=' /opt/asim-gcp/.env; then echo "`$SECRET" | sudo tee -a /opt/asim-gcp/.env >/dev/null; fi
grep -q '^XTREND_SOURCE=gaga' /opt/asim-gcp/.env || echo 'XTREND_SOURCE=gaga' | sudo tee -a /opt/asim-gcp/.env >/dev/null
# Keep bridge bundle copy of bridge for auto-update consumers
sudo mkdir -p /opt/asim-gcp/bridge-bundle
sudo systemctl restart asim-gcp
sleep 2
systemctl is-active asim-gcp
grep -E '^(XTREND_SOURCE|XTREND_GATE|ENTRY_BAR_MODE)=' /opt/asim-gcp/.env
grep -n '_gaga_trend\|XTREND_SOURCE\|gaga_ha' /opt/asim-gcp/bridge_trader.py | head -n 8
"@

Write-Host "=== Publish bridge bundle (both desks for URL, Ali uses gaga via .env) ===" -ForegroundColor Cyan
& (Join-Path (Split-Path -Parent $Root) "migration console\scripts\publish_to_gcp.ps1")

Write-Host "=== Ali Windows MT5 bridge ===" -ForegroundColor Cyan
$RepoRoot = Split-Path -Parent $Root
$mq5 = Join-Path $RepoRoot "migration\OnyxionXTrendProxy.mq5"
try {
  & $Gcloud compute scp (Join-Path $App "bridge_trader.py") "${Win}:bridge_trader.py" --zone=$Zone --project=$Project
  if (Test-Path $mq5) {
    & $Gcloud compute scp $mq5 "${Win}:OnyxionXTrendProxy.mq5" --zone=$Zone --project=$Project
  }
  $cmd = @"
`$root='$WinRoot'
`$src = Get-ChildItem -Path `$env:USERPROFILE,`$PWD,C:\tmp -Filter bridge_trader.py -Recurse -Depth 2 -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not `$src) { throw 'bridge_trader.py upload not found' }
New-Item -ItemType Directory -Force -Path `$root | Out-Null
Copy-Item -Force `$src.FullName (Join-Path `$root 'bridge_trader.py')
Write-Host ('bridge from ' + `$src.FullName)
`$mq = Get-ChildItem -Path `$env:USERPROFILE,`$PWD,C:\tmp -Filter OnyxionXTrendProxy.mq5 -Recurse -Depth 2 -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (`$mq) {
  `$ind = Join-Path `$root 'MQL5\Indicators\X Trend'
  New-Item -ItemType Directory -Force -Path `$ind | Out-Null
  Copy-Item -Force `$mq.FullName (Join-Path `$ind 'OnyxionXTrendProxy.mq5')
  Write-Host ('mq5 to ' + `$ind)
}
`$envPath = Join-Path `$root '.env'
if (Test-Path `$envPath) {
  `$txt = Get-Content `$envPath -Raw
  if (`$txt -match '(?m)^XTREND_SOURCE=') { `$txt = [regex]::Replace(`$txt, '(?m)^XTREND_SOURCE=.*$', 'XTREND_SOURCE=gaga') }
  else { `$txt = `$txt.TrimEnd() + "`r`nXTREND_SOURCE=gaga`r`n" }
  `$utf8 = New-Object System.Text.UTF8Encoding `$false
  [IO.File]::WriteAllText(`$envPath, `$txt, `$utf8)
  Write-Host 'env XTREND_SOURCE=gaga'
  Select-String -Path `$envPath -Pattern '^XTREND_SOURCE=' | ForEach-Object { Write-Host `$_.Line }
}
Select-String -Path (Join-Path `$root 'bridge_trader.py') -Pattern '_gaga_trend|gaga_ha' | Select-Object -First 2 | ForEach-Object { Write-Host `$_.Line.Trim().Substring(0,[Math]::Min(90,`$_.Line.Trim().Length)) }
Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |
  Where-Object { `$_.CommandLine -like '*watchdog*' -or `$_.CommandLine -like '*bridge_trader*' } |
  ForEach-Object { Stop-Process -Id `$_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2
try { Start-ScheduledTask -TaskName '$WinTask' } catch { Write-Host ('task warn: ' + `$_.Exception.Message) }
Start-Sleep 8
Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |
  Where-Object { `$_.CommandLine -like '*watchdog*' -or `$_.CommandLine -like '*bridge*' } |
  ForEach-Object { Write-Host ('python pid=' + `$_.ProcessId) }
"@
  & $Gcloud compute ssh $Win --zone=$Zone --project=$Project --command=$cmd
} catch {
  Write-Host "Windows push failed (SSH key?). Bundle is on Ali desk for auto-update." -ForegroundColor Yellow
  Write-Host $_.Exception.Message
}

Write-Host "=== DONE ===" -ForegroundColor Green
