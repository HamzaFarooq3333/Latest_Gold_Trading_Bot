#Requires -Version 5.1
<#
  Permanent public access for Ali live desk.
  Run after: gcloud auth login
#>
param(
  [string]$Project = "agent-gold-507217",
  [string]$Zone = "us-central1-a",
  [string]$Instance = "instance-20260831-171822",
  [string]$Gcloud = "D:\Google Cloud\google-cloud-sdk\bin\gcloud.ps1"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Gcloud)) { $Gcloud = "gcloud" }

Write-Host "=== Open TCP 80/443 to the world (0.0.0.0/0) ===" -ForegroundColor Cyan
& $Gcloud compute firewall-rules update allow-asim-live-443 --project=$Project --source-ranges=0.0.0.0/0
& $Gcloud compute firewall-rules update allow-asim-live-80 --project=$Project --source-ranges=0.0.0.0/0

Write-Host "=== Ensure VM running ===" -ForegroundColor Cyan
$status = & $Gcloud compute instances describe $Instance --zone=$Zone --project=$Project --format="value(status)"
if ($status -ne "RUNNING") {
  & $Gcloud compute instances start $Instance --zone=$Zone --project=$Project
}

$script = @'
set -eu
systemctl enable asim-gcp nginx >/dev/null 2>&1 || true
mkdir -p /etc/systemd/system/asim-gcp.service.d
cat >/etc/systemd/system/asim-gcp.service.d/override.conf <<EOF
[Service]
Restart=always
RestartSec=3
StartLimitIntervalSec=0
EOF
cat >/usr/local/bin/asim-gcp-healthcheck.sh <<EOF
#!/bin/bash
code=\$(curl -sk --max-time 8 -o /dev/null -w "%{http_code}" https://127.0.0.1/live || echo 000)
case "\$code" in 200|301|302|401|403) exit 0 ;; esac
logger -t asim-gcp-health "FAIL \$code restart"
systemctl restart asim-gcp
systemctl restart nginx || true
EOF
chmod +x /usr/local/bin/asim-gcp-healthcheck.sh
echo "* * * * * root /usr/local/bin/asim-gcp-healthcheck.sh >/dev/null 2>&1" >/etc/cron.d/asim-gcp-health
systemctl daemon-reload
systemctl restart asim-gcp
systemctl restart nginx || true
sleep 2
systemctl is-active asim-gcp
curl -sk -o /dev/null -w "https=%{http_code}\n" https://127.0.0.1/live
'@
$tmp = Join-Path $env:TEMP "keep_alive_remote.sh"
[System.IO.File]::WriteAllText($tmp, ($script -replace "`r`n","`n" -replace "`r","`n"))
& $Gcloud compute scp $tmp "${Instance}:/tmp/keep_alive_remote.sh" --zone=$Zone --project=$Project
& $Gcloud compute ssh $Instance --zone=$Zone --project=$Project --command="sed -i 's/\r$//' /tmp/keep_alive_remote.sh; sudo bash /tmp/keep_alive_remote.sh"

Write-Host "=== Probe ===" -ForegroundColor Cyan
[Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
foreach ($u in @("https://35.253.21.246/live","http://35.253.21.246/live")) {
  try {
    $r = Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 20 -MaximumRedirection 0 -ErrorAction Stop
    Write-Host "OK $($r.StatusCode) $u"
  } catch {
    if ($_.Exception.Response) { Write-Host "HTTP $([int]$_.Exception.Response.StatusCode) $u" }
    else { Write-Host "FAIL $u $($_.Exception.Message)" }
  }
}
Write-Host "Use: https://35.253.21.246/live  (ali / 123451)" -ForegroundColor Green
