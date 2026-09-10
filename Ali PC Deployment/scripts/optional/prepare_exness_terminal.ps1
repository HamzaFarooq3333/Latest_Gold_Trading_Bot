[CmdletBinding()]
param(
  [string]$SourceTerminalPath = "",
  [string]$InstallRoot = "C:\onyxion\exness-mt5",
  [string]$InstallerPath = "C:\onyxion\installers\exness-mt5.exe"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null

if (Test-Path $InstallerPath) {
  $signature = Get-AuthenticodeSignature -FilePath $InstallerPath
  if ($signature.Status -ne "Valid" -or
      $signature.SignerCertificate.Subject -notmatch "MetaQuotes|Exness") {
    throw "Refusing an unsigned or unexpected MT5 installer: $InstallerPath"
  }
  Write-Host "Verified installer present; using the isolated terminal copy until interactive installation is completed."
}

if (-not $SourceTerminalPath) {
  $candidates = @(
    "C:\Program Files\Exness MetaTrader 5\terminal64.exe",
    "C:\Program Files\MetaTrader 5\terminal64.exe",
    "C:\Program Files (x86)\MetaTrader 5\terminal64.exe"
  )
  $SourceTerminalPath = $candidates |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1
}

if (-not $SourceTerminalPath -or -not (Test-Path -LiteralPath $SourceTerminalPath)) {
  throw "No MT5 terminal found. Install the official Exness terminal or provide -SourceTerminalPath."
}

$sourceRoot = Split-Path -Parent $SourceTerminalPath
$targetRoot = $InstallRoot
if ((Resolve-Path $sourceRoot).Path -eq (Resolve-Path $targetRoot).Path) {
  throw "Source terminal must not already be the dedicated target: $InstallRoot"
}
if (-not (Test-Path (Join-Path $InstallRoot "terminal64.exe"))) {
  Get-ChildItem -LiteralPath $sourceRoot -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $targetRoot -Recurse -Force
  }
}

$targetTerminal = Join-Path $InstallRoot "terminal64.exe"
if (-not (Test-Path $targetTerminal)) {
  throw "Dedicated terminal copy was not created at $targetTerminal"
}

# /portable keeps this account's terminal data separate from the old Vantage
# terminal profile. Credentials remain in C:\onyxion\.env.
New-Item -ItemType Directory -Force -Path "C:\onyxion\logs","C:\onyxion\state","$InstallRoot\MQL5\Files" | Out-Null
Write-Host "Prepared dedicated portable MT5 terminal: $targetTerminal"
