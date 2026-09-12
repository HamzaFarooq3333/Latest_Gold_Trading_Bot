#Requires -Version 5.1
<#
  TRIGGER: When Ali pushes to Latest_Gold_Trading_Bot -> auto-update GCP live desk.

  Runs on Hamza's office PC (needs gh + SSH/gcloud to the Ali VM).
  Ali never redeploys GCP himself - this watcher does it for him.

  Flow:
    Ali git push Latest
      -> this script detects author=Ali within ~30s
      -> safety_gate
      -> apply into local workspace (so Hamza can clone/sync later)
      -> GCP install.sh redeploy (gcloud, or SSH key fallback)

  Source of truth: HamzaFarooq3333/Latest_Gold_Trading_Bot only.
#>
[CmdletBinding()]
param(
  [int]$PollSeconds = 30,
  [int]$WaitAfterDetectSeconds = 15,
  [string]$Workspace = "d:\Company\Onyxion\Week 7\New folder\New folder\New folder",
  [string]$CloneDir = "",
  [string]$Repo = "HamzaFarooq3333/Latest_Gold_Trading_Bot",
  [string[]]$AliLogins = @(
    "alimacbookpro-web",
    "ali",
    "Ali",
    "alimacbookpro"
  ),
  [switch]$Once,
  [switch]$IncludeAnyAuthor
)

$ErrorActionPreference = "Stop"
if (-not $CloneDir) {
  $CloneDir = Join-Path $env:TEMP "onyxion-ali-watch-clone"
}
$StateFile = Join-Path $env:TEMP "onyxion-ali-github-watch-state.json"
$StatusFile = Join-Path $Workspace "Google Console Deployment\logs\ali_push_deploy_status.json"
$LogFile = Join-Path $Workspace "Google Console Deployment\logs\ali_github_watch.log"
New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

$Gcloud = "D:\Google Cloud\google-cloud-sdk\bin\gcloud.ps1"
$Project = "agent-gold-507217"
$Zone = "us-central1-a"
$AliInstance = "instance-20260831-171822"
$RemoteDir = "/tmp/asim-gcp-deploy"
$SshKey = Join-Path $env:USERPROFILE ".ssh\google_compute_engine"
$SshTarget = "Lenovo@35.253.21.246"

function Write-WatchLog([string]$msg) {
  $line = "{0} {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss"), $msg
  Add-Content -Path $LogFile -Value $line -Encoding UTF8
  Write-Host $line
}

function Write-Status($obj) {
  try {
    ($obj | ConvertTo-Json -Depth 8) | Set-Content $StatusFile -Encoding UTF8
  } catch { }
}

function Load-State {
  if (Test-Path $StateFile) {
    try {
      $s = Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($null -eq $s) { return [pscustomobject]@{ last_sha = $null } }
      # Migrate older shape { seen = { repo = sha } }
      if (-not ($s.PSObject.Properties.Name -contains "last_sha")) {
        $migrated = $null
        if ($s.seen -and $s.seen.$Repo) { $migrated = [string]$s.seen.$Repo }
        elseif ($s.seen) {
          $props = $s.seen.PSObject.Properties
          if ($props -and $props.Count -gt 0) { $migrated = [string]$props[0].Value }
        }
        return [pscustomobject]@{ last_sha = $migrated }
      }
      return [pscustomobject]@{ last_sha = $s.last_sha }
    } catch { }
  }
  return [pscustomobject]@{ last_sha = $null }
}

function Save-State($state) {
  $blob = [pscustomobject]@{ last_sha = [string]$state.last_sha }
  ($blob | ConvertTo-Json -Depth 8) | Set-Content $StateFile -Encoding UTF8
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

function Test-IsAliCommit($info) {
  if ($IncludeAnyAuthor) { return $true }
  if (-not $info) { return $false }
  $login = ([string]$info.login).Trim().ToLower()
  $name = ([string]$info.name).Trim().ToLower()
  $email = ([string]$info.email).Trim().ToLower()
  foreach ($a in $AliLogins) {
    $al = $a.ToLower()
    if ($login -eq $al) { return $true }
    if ($name -like ("*" + $al + "*")) { return $true }
    if ($email -like ("*" + $al + "*")) { return $true }
  }
  if ($login -match "alimacbook") { return $true }
  if ($name -match '(^|[^a-z])ali([^a-z]|$)') { return $true }
  return $false
}

function Invoke-LocalSafetyGate([string]$checkRoot) {
  $gate = Join-Path $Workspace "Google Console Deployment\safety_gate.ps1"
  if (-not (Test-Path $gate)) {
    $gate = Join-Path $Workspace "Ali PC Deployment\scripts\safety_gate.ps1"
  }
  if (-not (Test-Path $gate)) {
    Write-WatchLog "WARN safety_gate.ps1 missing - continuing"
    return $true
  }
  Write-WatchLog ("safety_gate " + $checkRoot)
  & powershell -NoProfile -ExecutionPolicy Bypass -File $gate -Root $checkRoot
  if ($LASTEXITCODE -ne 0) {
    Write-WatchLog "safety_gate FAILED - skip GCP redeploy"
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
    @{ From = "Google Console Deployment\install.sh"; To = (Join-Path $Workspace "Google Console Deployment\install.sh") },
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

function Deploy-ViaSsh {
  $App = Join-Path $Workspace "Google Console Deployment\app"
  $Root = Join-Path $Workspace "Google Console Deployment"
  $envFile = Join-Path $Root "env.instance-20260831-171822"
  if (-not (Test-Path $SshKey)) {
    Write-WatchLog ("ERROR: SSH key missing " + $SshKey)
    return $false
  }
  if (-not (Test-Path $envFile)) {
    Write-WatchLog ("ERROR: missing " + $envFile)
    return $false
  }
  Write-WatchLog ("SSH deploy to " + $SshTarget)
  $sshBase = @("-i", $SshKey, "-o", "StrictHostKeyChecking=no", $SshTarget)
  & ssh @sshBase "sudo rm -rf $RemoteDir && sudo mkdir -p $RemoteDir/app && sudo chmod -R 777 $RemoteDir"
  if ($LASTEXITCODE -ne 0) { throw "ssh prep failed" }
  & scp -i $SshKey -o StrictHostKeyChecking=no -r "$App\*" "${SshTarget}:${RemoteDir}/app/"
  if ($LASTEXITCODE -ne 0) { throw "scp app failed" }
  foreach ($f in @("install.sh", "asim-gcp.service", "nginx-asim-gcp.conf")) {
    $local = Join-Path $Root $f
    if (Test-Path $local) {
      & scp -i $SshKey -o StrictHostKeyChecking=no $local "${SshTarget}:${RemoteDir}/$f"
    }
  }
  & scp -i $SshKey -o StrictHostKeyChecking=no $envFile "${SshTarget}:${RemoteDir}/.env"
  & ssh @sshBase "cd $RemoteDir && sed -i 's/\r$//' install.sh && chmod +x install.sh && bash install.sh"
  if ($LASTEXITCODE -ne 0) { throw "install.sh failed" }
  Write-WatchLog "SSH GCP dashboard deploy OK -> https://35.253.21.246/live"
  return $true
}

function Deploy-AliGcpDashboard {
  $App = Join-Path $Workspace "Google Console Deployment\app"
  $Root = Join-Path $Workspace "Google Console Deployment"
  $envFile = Join-Path $Root "env.instance-20260831-171822"
  if (-not (Test-Path $envFile)) {
    Write-WatchLog ("ERROR: missing " + $envFile)
    return $false
  }

  # Prefer gcloud when available; fall back to direct SSH (proven path).
  if (Test-Path $Gcloud) {
    Write-WatchLog ("gcloud deploy to " + $AliInstance)
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
      Write-WatchLog ("gcloud deploy failed: " + $_.Exception.Message + " - trying SSH fallback")
    }
  } else {
    Write-WatchLog "gcloud missing - using SSH fallback"
  }

  try {
    return (Deploy-ViaSsh)
  } catch {
    Write-WatchLog ("ERROR SSH deploy: " + $_.Exception.Message)
    return $false
  }
}

function Process-AliPush($info) {
  $short = $info.sha.Substring(0, 7)
  $firstMsg = (($info.msg -split "`n")[0])
  Write-WatchLog ("ALI PUSH DETECTED repo=$Repo sha=$short by $($info.login)/$($info.name) msg=$firstMsg")
  Write-Status @{
    event = "ali_push_detected"
    sha = $info.sha
    author = "$($info.login)/$($info.name)"
    message = $firstMsg
    detected_utc = (Get-Date).ToUniversalTime().ToString("o")
    gcp_deployed = $false
  }
  Write-WatchLog ("waiting $WaitAfterDetectSeconds s...")
  Start-Sleep -Seconds $WaitAfterDetectSeconds

  $url = "https://github.com/$Repo.git"
  $applied = Apply-LocalFromClone -repoUrl $url -sha $info.sha
  if (-not $applied) {
    Write-WatchLog "local apply blocked - desk not redeployed"
    Write-Status @{
      event = "blocked_safety_gate"
      sha = $info.sha
      detected_utc = (Get-Date).ToUniversalTime().ToString("o")
      gcp_deployed = $false
    }
    return
  }

  $verPath = Join-Path $Workspace "Ali PC Deployment\runtime\VERSION.json"
  if (Test-Path $verPath) {
    try {
      $ver = Get-Content $verPath -Raw -Encoding UTF8 | ConvertFrom-Json
      $ver | Add-Member -NotePropertyName last_ali_github_sha -NotePropertyValue $info.sha -Force
      $ver | Add-Member -NotePropertyName last_ali_github_repo -NotePropertyValue $Repo -Force
      $ver | Add-Member -NotePropertyName last_ali_applied_utc -NotePropertyValue ((Get-Date).ToUniversalTime().ToString("o")) -Force
      ($ver | ConvertTo-Json -Depth 6) | Set-Content $verPath -Encoding UTF8
    } catch { }
  }

  $ok = Deploy-AliGcpDashboard
  Write-WatchLog ("Ali auto-deploy complete gcp_ok=$ok sha=$short")
  Write-Status @{
    event = "ali_push_deployed"
    sha = $info.sha
    author = "$($info.login)/$($info.name)"
    message = $firstMsg
    deployed_utc = (Get-Date).ToUniversalTime().ToString("o")
    gcp_deployed = [bool]$ok
    live_url = "https://35.253.21.246/live"
    clone_when_back = "git clone https://github.com/$Repo.git"
  }
}

Write-WatchLog "Ali->GCP auto-deploy watcher start poll=${PollSeconds}s repo=$Repo"
$state = Load-State
$info = Get-LatestSha $Repo
if ($info) {
  Write-WatchLog ("baseline sha=$($info.sha.Substring(0,7)) author=$($info.login)/$($info.name) isAli=$(Test-IsAliCommit $info)")
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
      if (Test-IsAliCommit $info) {
        Process-AliPush $info
      } else {
        Write-WatchLog ("ignoring non-Ali author $($info.login)/$($info.name) (use watch_hamza_push_deploy.ps1 for Hamza)")
      }
    }
  } catch {
    Write-WatchLog ("poll error: " + $_.Exception.Message)
  }
  if ($Once) { break }
  Start-Sleep -Seconds $PollSeconds
} while ($true)

Write-WatchLog "watcher exit"
