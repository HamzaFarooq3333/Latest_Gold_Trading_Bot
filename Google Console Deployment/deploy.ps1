# Deploy Asim live desk to both Agent Gold VMs (HTTPS + IP firewall + login)
$ErrorActionPreference = "Stop"
$env:CLOUDSDK_COMPUTE_SSH = "ssh"
$env:CLOUDSDK_COMPUTE_SCP = "scp"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = Join-Path $Root "app"
$Gcloud = "D:\Google Cloud\google-cloud-sdk\bin\gcloud.ps1"
$Project = "agent-gold-507217"
$Zone = "us-central1-a"
$Instances = @("hamzatestserver01", "instance-20260831-171822")
$RemoteDir = "/tmp/asim-gcp-deploy"
# Comma-separated CIDRs allowed to reach HTTPS :443 on asim-live VMs.
# Add each bridge laptop's public IP as /32 (friend: check https://ifconfig.me).
$AllowedSourceIps = @(
  "103.11.61.0/24",    # office network
  "86.97.80.177/32",   # prior public IP
  "103.11.61.90/32",   # current office IP (2026-09-02)
  "35.253.21.246/32",  # hamza-mt5-windows
  "34.10.216.61/32"    # ali-mt5-windows
)
$AllowedSourceIp = ($AllowedSourceIps -join ",")

if (-not (Test-Path $Gcloud)) {
  throw "gcloud not found at $Gcloud"
}

function Get-InstanceEnvFile {
  param([string]$Instance)
  $map = @{
    "hamzatestserver01" = "env.hamzatestserver01"
    "instance-20260831-171822" = "env.instance-20260831-171822"
  }
  $file = Join-Path $Root $map[$Instance]
  if (-not (Test-Path $file)) { throw "Missing env file for $Instance" }
  return $file
}

Write-Host "=== Firewall: HTTPS 443 from $AllowedSourceIp ===" -ForegroundColor Cyan
$rule443 = "allow-asim-live-443"
$existing443 = & $Gcloud compute firewall-rules list --project=$Project --filter="name=$rule443" --format="value(name)" 2>$null
if ($existing443) {
  & $Gcloud compute firewall-rules update $rule443 `
    --project=$Project `
    --source-ranges=$AllowedSourceIp `
    --rules="tcp:443" `
    --target-tags=asim-live | Out-Null
} else {
  & $Gcloud compute firewall-rules create $rule443 `
    --project=$Project `
    --direction=INGRESS `
    --priority=900 `
    --network=default `
    --action=ALLOW `
    --rules="tcp:443" `
    --source-ranges=$AllowedSourceIp `
    --target-tags=asim-live | Out-Null
}

# Remove open HTTP 8080 rule if present (app is localhost-only now)
$rule8080 = "allow-asim-live-8080"
$existing8080 = & $Gcloud compute firewall-rules list --project=$Project --filter="name=$rule8080" --format="value(name)" 2>$null
if ($existing8080) {
  Write-Host "Removing public HTTP 8080 rule (replaced by HTTPS 443)" -ForegroundColor Yellow
  & $Gcloud compute firewall-rules delete $rule8080 --project=$Project --quiet | Out-Null
}

foreach ($instance in $Instances) {
  Write-Host "=== Deploy $instance ===" -ForegroundColor Cyan
  $envFile = Get-InstanceEnvFile -Instance $instance

  & $Gcloud compute instances add-tags $instance --zone=$Zone --project=$Project --tags=asim-live | Out-Null
  & $Gcloud compute ssh $instance --zone=$Zone --project=$Project --command="sudo rm -rf $RemoteDir && sudo mkdir -p $RemoteDir/app && sudo chmod -R 777 $RemoteDir"
  & $Gcloud compute scp --recurse "$App" "${instance}:${RemoteDir}/" --zone=$Zone --project=$Project
  & $Gcloud compute scp (Join-Path $Root "install.sh") "${instance}:${RemoteDir}/install.sh" --zone=$Zone --project=$Project
  & $Gcloud compute scp (Join-Path $Root "asim-gcp.service") "${instance}:${RemoteDir}/asim-gcp.service" --zone=$Zone --project=$Project
  & $Gcloud compute scp (Join-Path $Root "nginx-asim-gcp.conf") "${instance}:${RemoteDir}/nginx-asim-gcp.conf" --zone=$Zone --project=$Project
  & $Gcloud compute scp $envFile "${instance}:${RemoteDir}/.env" --zone=$Zone --project=$Project
  & $Gcloud compute ssh $instance --zone=$Zone --project=$Project --command="cd $RemoteDir && sed -i 's/\r$//' install.sh && chmod +x install.sh && bash install.sh"

  $ip = & $Gcloud compute instances describe $instance --zone=$Zone --project=$Project --format="get(networkInterfaces[0].accessConfigs[0].natIP)"
  Write-Host "Live desk: https://${ip}/live" -ForegroundColor Green
  Write-Host "Login: see env file for this instance (hamza or ali / 123451)" -ForegroundColor Green
  Write-Host "SSH tunnel: .\ssh-tunnel.ps1 -Instance $instance" -ForegroundColor Cyan
}

Write-Host "Done. Firewall allows only $AllowedSourceIp on port 443." -ForegroundColor Green
