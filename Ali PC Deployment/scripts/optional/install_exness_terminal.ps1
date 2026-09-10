[CmdletBinding()]
param(
  [string]$InstallerPath = "C:\onyxion\installers\exness5setup.exe",
  [string]$InstallRoot = "C:\onyxion\exness-mt5",
  [string]$InstallerUri = "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstallerPath) | Out-Null
if (-not (Test-Path -LiteralPath $InstallerPath)) {
  Write-Host "Downloading the official MetaTrader 5 installer."
  Invoke-WebRequest -Uri $InstallerUri -OutFile $InstallerPath -UseBasicParsing
}

$signature = Get-AuthenticodeSignature -FilePath $InstallerPath
if ($signature.Status -ne "Valid" -or
    $signature.SignerCertificate.Subject -notmatch "MetaQuotes|Exness") {
  throw "Refusing an unsigned or unexpected MT5 installer: $InstallerPath"
}

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
Write-Host "Verified installer: $($signature.SignerCertificate.Subject)"
Write-Host "Installing the isolated MT5 terminal into $InstallRoot."
$process = Start-Process -FilePath $InstallerPath `
  -ArgumentList "/auto", "/path:`"$InstallRoot`"" `
  -Wait -PassThru
if ($process.ExitCode -ne 0 -and
    -not (Test-Path -LiteralPath (Join-Path $InstallRoot "terminal64.exe"))) {
  throw "MT5 installer failed with exit code $($process.ExitCode)"
}
if ($process.ExitCode -ne 0) {
  Write-Warning "MT5 installer returned $($process.ExitCode), but terminal64.exe is present."
}

$terminal = Join-Path $InstallRoot "terminal64.exe"
if (-not (Test-Path -LiteralPath $terminal)) {
  throw "Installer finished but terminal64.exe was not found at $terminal"
}

Write-Host "Exness MT5 installed at $terminal"
