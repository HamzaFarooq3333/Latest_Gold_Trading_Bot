#Requires -Version 5.1
<#
  Launch the portable Exness MT5 terminal and auto-log it into Ali's demo
  account.

  Credentials are read from C:\onyxion-ali\.env at run time, so the password
  never appears in a Scheduled Task argument list, in the process command line
  of the caller, or in any log written by this script.
#>
[CmdletBinding()]
param(
  [string]$Root = "C:\onyxion-ali",
  [switch]$Force
)

$ErrorActionPreference = "Stop"
$envPath = Join-Path $Root ".env"
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "connection_check.log"

function Write-Line($level, $msg) {
  $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss")
  Add-Content -Path $logFile -Value "$ts UTC [$level] launch_mt5: $msg" -Encoding UTF8
}

function Read-EnvValue($name) {
  foreach ($raw in Get-Content $envPath -Encoding UTF8) {
    $line = $raw.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line -match "=") {
      $k, $v = $line.Split("=", 2)
      if ($k.Trim() -eq $name) { return $v.Trim().Trim('"').Trim("'") }
    }
  }
  return $null
}

$terminal = Read-EnvValue "ASIM_MT5_TERMINAL_PATH"
if (-not $terminal) { $terminal = Join-Path $Root "exness-mt5\terminal64.exe" }
if (-not (Test-Path $terminal)) {
  Write-Line "ERROR" "terminal missing: $terminal"
  throw "terminal missing: $terminal"
}
$termRoot = Split-Path -Parent $terminal

# Already running? Leave it alone unless -Force.
$running = Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
  Where-Object { $_.CommandLine -like "*$termRoot*" } | Select-Object -First 1
if ($running -and -not $Force) {
  Write-Line "INFO" "already running pid=$($running.ProcessId)"
  Write-Host "MT5 already running pid=$($running.ProcessId)"
  exit 0
}
if ($running -and $Force) {
  Write-Line "WARN" "force restart, killing pid=$($running.ProcessId)"
  Stop-Process -Id $running.ProcessId -Force
  Start-Sleep -Seconds 5
}

$login = Read-EnvValue "ASIM_MT5_LOGIN"
$password = Read-EnvValue "ASIM_MT5_PASSWORD"
$server = Read-EnvValue "ASIM_MT5_SERVER"
if (-not $login -or -not $password -or -not $server) {
  Write-Line "ERROR" "missing MT5 credentials in .env"
  throw "missing MT5 credentials in .env"
}

# /portable keeps this terminal isolated from any other MT5 install on the PC.
$mt5Args = @("/portable", "/login:$login", "/password:$password", "/server:$server")
Start-Process -FilePath $terminal -ArgumentList $mt5Args -WorkingDirectory $termRoot -WindowStyle Minimized
Start-Sleep -Seconds 20

$now = Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
  Where-Object { $_.CommandLine -like "*$termRoot*" } | Select-Object -First 1
if ($now) {
  # Never log the credential values themselves — only the account number.
  Write-Line "INFO" "launched pid=$($now.ProcessId) login=$login server=$server"
  Write-Host "MT5 launched pid=$($now.ProcessId) login=$login"
  exit 0
}
Write-Line "ERROR" "launch failed — no terminal64.exe after 20s"
throw "MT5 launch failed"
