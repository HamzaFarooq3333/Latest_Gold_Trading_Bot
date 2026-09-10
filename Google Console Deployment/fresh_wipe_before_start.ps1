# Wipe desk history + clear local engine state, then start bridge in RDP session.
# Call before launching the Python interpreter so we always start from scratch.
param(
  [ValidateSet("hamza","ali")]
  [string]$Profile = "hamza"
)

$ErrorActionPreference = "Stop"
if ($Profile -eq "hamza") {
  $Root = "C:\onyxion-hamza"
  $DeskUrl = "https://35.232.76.12"
  $TaskName = "OnyxionBridgeStack-hamza"
  $StartLocal = "C:\ProgramData\Onyxion\start_hamza_interactive.ps1"
} else {
  $Root = "C:\onyxion-ali"
  $DeskUrl = "https://35.223.235.204"
  $TaskName = "OnyxionBridgeStack-ali"
  $StartLocal = "C:\ProgramData\Onyxion\start_ali_interactive.ps1"
}

Write-Host "=== Fresh wipe for $Profile ==="

# Stop any running bridge first
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*watchdog*" -or $_.CommandLine -like "*bridge_trader*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null

# Clear local logs + engine state
New-Item -ItemType Directory -Force -Path "$Root\logs","$Root\state" | Out-Null
Get-ChildItem "$Root\logs" -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem "$Root\state" -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
@("engine_state.json","mt5_live_state.json","asim_state.json","demo_state.json") | ForEach-Object {
  $p = Join-Path $Root $_
  if (Test-Path $p) { Remove-Item $p -Force -ErrorAction SilentlyContinue }
}

# Reset GCP desk broker store
try {
  Add-Type @"
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public class TrustAll { public static bool CB(object s, X509Certificate c, X509Chain ch, SslPolicyErrors e){ return true; } }
"@ -ErrorAction SilentlyContinue
  [System.Net.ServicePointManager]::ServerCertificateValidationCallback = [TrustAll]::CB
  [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
  $null = Invoke-RestMethod -Method POST -Uri "$DeskUrl/api/broker/reset" -ContentType "application/json" -Body "{}" -TimeoutSec 30
  Write-Host "Desk reset OK: $DeskUrl"
} catch {
  Write-Host "WARN desk reset failed: $($_.Exception.Message)"
}

Write-Host "Local state cleared. Start bridge with: $StartLocal"
Write-Host "FRESH_WIPE_DONE"
