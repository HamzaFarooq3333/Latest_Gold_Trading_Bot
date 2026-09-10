#Requires -Version 5.1
# ONE COMMAND after you finish Google login:
#   powershell -ExecutionPolicy Bypass -File ".\Google Console Deployment\open_public_firewall_permanent.ps1"
#
# Or paste this into an elevated PowerShell AFTER: gcloud auth login

$ErrorActionPreference = "Stop"
$Gcloud = if (Test-Path "D:\Google Cloud\google-cloud-sdk\bin\gcloud.cmd") {
  "D:\Google Cloud\google-cloud-sdk\bin\gcloud.cmd"
} else { "gcloud" }

Write-Host "Testing auth..."
& $Gcloud auth print-access-token | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Run this first in the same window:" -ForegroundColor Yellow
  Write-Host "  gcloud auth login hamza.farooq@onyxion.io --update-adc --force"
  exit 1
}

$Project = "agent-gold-507217"
Write-Host "Opening firewall 80+443 to 0.0.0.0/0 (public)..."
& $Gcloud compute firewall-rules update allow-asim-live-443 --project=$Project --source-ranges=0.0.0.0/0
& $Gcloud compute firewall-rules update allow-asim-live-80 --project=$Project --source-ranges=0.0.0.0/0
& $Gcloud compute firewall-rules describe allow-asim-live-443 --project=$Project --format="table(name,sourceRanges.list(),allowed[].map().firewall_rule().list())"
& $Gcloud compute firewall-rules describe allow-asim-live-80 --project=$Project --format="table(name,sourceRanges.list(),allowed[].map().firewall_rule().list())"

Write-Host ""
Write-Host "Done. Open: https://35.253.21.246/live   (ali / 123451)" -ForegroundColor Green
Write-Host "Use https://  — not bare http IP without scheme if Chrome acts up."
