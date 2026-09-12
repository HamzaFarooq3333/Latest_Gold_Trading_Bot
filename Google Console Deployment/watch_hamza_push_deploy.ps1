#Requires -Version 5.1
<#
  Hamza-only desk redeploy watcher.
  Watches HamzaFarooq3333/Latest_Gold_Trading_Bot for Hamza pushes (not Ali-only).
  On change: safety_gate → apply local workspace → GCP install.sh redeploy.
  Ali PC never runs this / never calls gcloud.
#>
[CmdletBinding()]
param(
  [int]$PollSeconds = 45,
  [int]$WaitAfterDetectSeconds = 10,
  [string]$Workspace = "d:\Company\Onyxion\Week 7\New folder\New folder\New folder",
  [string]$CloneDir = "",
  [string]$Repo = "HamzaFarooq3333/Latest_Gold_Trading_Bot",
  [string[]]$HamzaLogins = @("HamzaFarooq3333", "hamza", "Hamza"),
  [switch]$Once,
  [switch]$IncludeAnyAuthor
)

$ErrorActionPreference = "Stop"
if (-not $CloneDir) {
  $CloneDir = Join-Path $env:TEMP "onyxion-hamza-watch-clone"
}
$StateFile = Join-Path $env:TEMP "onyxion-hamza-push-deploy-state.json"
$LogFile = Join-Path $Workspace "Google Console Deployment\logs\hamza_push_deploy.log"
New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

$Gcloud = "D:\Google Cloud\google-cloud-sdk\bin\gcloud.ps1"
$Project = "agent-gold-507217"
$Zone = "us-central1-a"
$AliInstance = "instance-20260831-171822"
$RemoteDir = "/tmp/asim-gcp-deploy"

function Write-WatchLog([string]$msg) {
  $line = "{0} {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss"), $msg
  Add-Content -Path $LogFile -Value $line -Encoding UTF8
  Write-Host $line
}

function Load-State {
  if (Test-Path $StateFile) {
    try { return Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
  }
  return [pscustomobject]@{ last_sha = $null }
}

function Save-State($state) {
  ($state | ConvertTo-Json -Depth 8) | Set-Content $StateFile -Encoding UTF8
}

function Get-LatestSha([string]$repo) {
  $json = gh api "repos/$repo/commits?per_page=1" 2>$null
  if (-not $json) { return $null }
  $arr = $json | ConvertFrom-Json
  if (-not $arr) { return $null }
  $c = if ($arr -is [array]) { $arr[0] } else { $arr }
  return [pscustomobject]@{
    sha   = [string]$c.sha
    login = [string]$c.author.login
    name  = [string]$c.commit.author.name
    email = [string]$c.commit.author.email
    msg   = [string]$c.commit.message
    date  = [string]$c.commit.author.date
  }
}

function Test-IsHamzaCommit($info) {
  if ($IncludeAnyAuthor) { return $true }
  if (-not $info) { return $false }
  $login = ([string]$info.login).Trim().ToLower()
  $name = ([string]$info.name).Trim().ToLower()
  $email = ([string]$info.email).Trim().ToLower()
  foreach ($a in $HamzaLogins) {
    $al = $a.ToLower()
    if ($login -eq $al) { return $true }
    if ($name -like ("*" + $al + "*")) { return $true }
    if ($email -like ("*" + $al + "*")) { return $true }
  }
  if ($login -match "hamza") { return $true }
  return $false
}

function Invoke-LocalSafetyGate([string]$checkRoot) {
  $gate = Join-Path $Workspace "Google Console Deployment\safety_gate.ps1"
  if (-not (Test-Path $gate)) {
    $gate = Join-Path $Workspace "Ali PC Deployment\scripts\safety_gate.ps1"
  }
  if (-not (Test-Path $gate)) {
    Write-WatchLog "WARN safety_gate.ps1 missing"
    return $true
  }
  Write-WatchLog ("safety_gate " + $checkRoot)
  & powershell -NoProfile -ExecutionPolicy Bypass -File $gate -Root $checkRoot
  if ($LASTEXITCODE -ne 0) {
    Write-WatchLog "safety_gate FAILED — skip desk redeploy"
    return $false
  }
  Write-WatchLog "safety_gate PASS"
  return $true
}

function Apply-LocalFromClone([string]$repoUrl, [string]$sha) {
  if (Test-Path $CloneDir) { Remove-Item $CloneDir -Recurse -Force -ErrorAction SilentlyContinue }
  New-Item -ItemType Directory -Force -Path $CloneDir | Out-Null
  Write-WatchLog ("cloning " + $repoUrl + " @ " + $sha)
  git clone --depth 20 $repoUrl $CloneDir
  Push-Location $CloneDir
  try {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    git -c advice.detachedHead=false checkout --quiet $sha 2>$null | Out-Null
    $ErrorActionPreference = $prevEap
  } finally {
    Pop-Location
  }

  if (-not (Invoke-LocalSafetyGate -checkRoot $CloneDir)) {
    return $false
  }

  $pairs = @(
    @{ From = "Ali PC Deployment"; To = (Join-Path $Workspace "Ali PC Deployment") },
    @{ From = "Google Console Deployment\app"; To = (Join-Path $Workspace "Google Console Deployment\app") },
    @{ From = "TRADING_RULES.md"; To = (Join-Path $Workspace "TRADING_RULES.md") }
  )
  foreach ($p in $pairs) {
    $src = Join-Path $CloneDir $p.From
    if (-not (Test-Path $src)) {
      Write-WatchLog ("skip missing " + $p.From)
      continue
    }
    $dst = $p.To
    New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
    if ((Get-Item $src).PSIsContainer) {
      Copy-Item (Join-Path $src "*") $dst -Recurse -Force
    } else {
      Copy-Item $src $dst -Force
    }
    Write-WatchLog ("applied " + $p.From + " -> " + $dst)
  }
  return $true
}

function Deploy-AliGcpDashboard {
  if (-not (Test-Path $Gcloud)) {
    Write-WatchLog ("ERROR: gcloud missing at " + $Gcloud)
    return $false
  }
  $App = Join-Path $Workspace "Google Console Deployment\app"
  $Root = Join-Path $Workspace "Google Console Deployment"
  $envFile = Join-Path $Root "env.instance-20260831-171822"
  if (-not (Test-Path $envFile)) {
    Write-WatchLog ("ERROR: missing " + $envFile)
    return $false
  }

  Write-WatchLog ("deploying GCP dashboard to " + $AliInstance + " (Hamza-only path)")
  $env:CLOUDSDK_COMPUTE_SSH = "ssh"
  $env:CLOUDSDK_COMPUTE_SCP = "scp"
  try {
    $prep = "sudo rm -rf $RemoteDir && sudo mkdir -p $RemoteDir/app && sudo chmod -R 777 $RemoteDir"
    & $Gcloud compute ssh $AliInstance --zone=$Zone --project=$Project --command=$prep
    if ($LASTEXITCODE -ne 0) { throw "ssh prep failed" }
    & $Gcloud compute scp --recurse $App ($AliInstance + ":" + $RemoteDir + "/") --zone=$Zone --project=$Project
    if ($LASTEXITCODE -ne 0) { throw "scp app failed" }
    foreach ($f in @("install.sh", "asim-gcp.service", "nginx-asim-gcp.conf")) {
      $local = Join-Path $Root $f
      if (Test-Path $local) {
        & $Gcloud compute scp $local ($AliInstance + ":" + $RemoteDir + "/" + $f) --zone=$Zone --project=$Project
      }
    }
    & $Gcloud compute scp $envFile ($AliInstance + ":" + $RemoteDir + "/.env") --zone=$Zone --project=$Project
    $installCmd = 'cd ' + $RemoteDir + ' && sed -i "s/\r$//" install.sh && chmod +x install.sh && bash install.sh'
    & $Gcloud compute ssh $AliInstance --zone=$Zone --project=$Project --command=$installCmd
    if ($LASTEXITCODE -ne 0) { throw "install.sh failed" }
    Write-WatchLog "GCP dashboard deploy OK -> https://35.253.21.246/live"
    return $true
  } catch {
    Write-WatchLog ("ERROR GCP deploy: " + $_.Exception.Message)
    Write-WatchLog "HINT: gcloud auth login, or use SSH key deploy fallback"
    return $false
  }
}

function Process-HamzaPush($info) {
  $short = $info.sha.Substring(0, 7)
  $firstMsg = (($info.msg -split "`n")[0])
  Write-WatchLog ("HAMZA PUSH repo=$Repo sha=$short by $($info.login)/$($info.name) msg=$firstMsg")
  Write-WatchLog ("waiting $WaitAfterDetectSeconds s...")
  Start-Sleep -Seconds $WaitAfterDetectSeconds
  $url = "https://github.com/$Repo.git"
  $applied = Apply-LocalFromClone -repoUrl $url -sha $info.sha
  if (-not $applied) {
    Write-WatchLog "local apply blocked — desk not redeployed"
    return
  }
  $ok = Deploy-AliGcpDashboard
  Write-WatchLog ("hamza deploy complete gcp_ok=$ok")
}

Write-WatchLog "hamza desk watcher start poll=${PollSeconds}s repo=$Repo"
$state = Load-State
$info = Get-LatestSha $Repo
if ($info) {
  Write-WatchLog ("baseline sha=$($info.sha.Substring(0,7)) author=$($info.login)/$($info.name) isHamza=$(Test-IsHamzaCommit $info)")
  if (-not $state.last_sha) {
    $state.last_sha = $info.sha
    Save-State $state
  }
}

do {
  try {
    $info = Get-LatestSha $Repo
    if ($info -and $state.last_sha -ne $info.sha) {
      Write-WatchLog ("new tip $($info.sha.Substring(0,7)) by $($info.login)/$($info.name)")
      $state.last_sha = $info.sha
      Save-State $state
      if (Test-IsHamzaCommit $info) {
        Process-HamzaPush $info
      } else {
        Write-WatchLog ("ignoring non-Hamza author $($info.login)/$($info.name) (Ali PC agent handles bridge)")
      }
    }
  } catch {
    Write-WatchLog ("poll error: " + $_.Exception.Message)
  }
  if ($Once) { break }
  Start-Sleep -Seconds $PollSeconds
} while ($true)

Write-WatchLog "watcher exit"
