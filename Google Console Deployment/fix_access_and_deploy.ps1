# Restore dashboard access (current public IP drifts out of the 443 allowlist)
# and push the current engine to both Linux desks.
$ErrorActionPreference = "Stop"
$Gcloud = "D:\Google Cloud\google-cloud-sdk\bin\gcloud.ps1"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $Root "app"
$Zone = "us-central1-a"
$Project = "agent-gold-507217"
$Rule = "allow-asim-live-443"

if (-not (Test-Path $Gcloud)) { throw "gcloud not found at $Gcloud" }

$desks = @(
  @{ Name = "hamzatestserver01";        EnvFile = "env.hamzatestserver01";        User = "hamza"; Pass = "123451"; Url = "https://35.232.76.12" },
  @{ Name = "instance-20260831-171822"; EnvFile = "env.instance-20260831-171822"; User = "ali";   Pass = "123451"; Url = "https://35.223.235.204" }
)

# --- 1. firewall: add this machine's current public IP -----------------------
$myIp = (curl.exe -s https://ifconfig.me).Trim()
if ($myIp -notmatch '^\d+\.\d+\.\d+\.\d+$') { throw "could not resolve public IP (got '$myIp')" }
Write-Host "current public IP: $myIp" -ForegroundColor Cyan

$existing = (& $Gcloud compute firewall-rules describe $Rule --project=$Project --format="value(sourceRanges.list())")
$ranges = @($existing -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($ranges -contains "$myIp/32") {
  Write-Host "IP already allowed" -ForegroundColor Green
} else {
  $ranges += "$myIp/32"
  $joined = $ranges -join ','
  & $Gcloud compute firewall-rules update $Rule --project=$Project "--source-ranges=$joined"
  Write-Host "allowlist now: $joined" -ForegroundColor Green
}

# --- 2. confirm the desks are actually running -------------------------------
foreach ($d in $desks) {
  $state = (& $Gcloud compute instances describe $d.Name --zone=$Zone --project=$Project --format="value(status)")
  Write-Host "$($d.Name): $state"
  if ($state -ne "RUNNING") {
    Write-Host "  starting..." -ForegroundColor Yellow
    & $Gcloud compute instances start $d.Name --zone=$Zone --project=$Project
  }
}

# --- 3. push the engine ------------------------------------------------------
foreach ($d in $desks) {
  Write-Host "=== deploy $($d.Name) ===" -ForegroundColor Cyan
  & $Gcloud compute scp (Join-Path $App "server.py")           "$($d.Name):/tmp/server.py"           --zone=$Zone --project=$Project
  & $Gcloud compute scp (Join-Path $App "mt5_live_engine.py")  "$($d.Name):/tmp/mt5_live_engine.py"  --zone=$Zone --project=$Project
  & $Gcloud compute scp (Join-Path $App "broker_live.py")      "$($d.Name):/tmp/broker_live.py"      --zone=$Zone --project=$Project
  & $Gcloud compute scp (Join-Path $Root $d.EnvFile)           "$($d.Name):/tmp/asim.env"            --zone=$Zone --project=$Project
  & $Gcloud compute scp (Join-Path $Root "remote_install_best_engine.sh") "$($d.Name):/tmp/remote_install_best_engine.sh" --zone=$Zone --project=$Project
  & $Gcloud compute ssh $d.Name --zone=$Zone --project=$Project --command="sed -i 's/\r$//' /tmp/remote_install_best_engine.sh && bash /tmp/remote_install_best_engine.sh"
}

# --- 4. verify from here -----------------------------------------------------
foreach ($d in $desks) {
  $pair = $d.User + ":" + $d.Pass
  Write-Host "=== health $($d.Name) ===" -ForegroundColor Cyan
  curl.exe -k -s --max-time 20 -u $pair "$($d.Url)/health"
  Write-Host ""
}

Write-Host "=== DONE ===" -ForegroundColor Green
