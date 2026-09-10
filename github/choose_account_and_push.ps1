#Requires -Version 5.1
<#
  Choose which GitHub account to use, then push the live pack
  (bridge + MQ5 + dashboard + TRADING_RULES) to a repo you pick.

  Run:
    powershell -ExecutionPolicy Bypass -File .\github\choose_account_and_push.ps1
#>
param(
  [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $here }
Set-Location $RepoRoot

function Show-Accounts {
  Write-Host ""
  Write-Host "=== GitHub accounts currently logged in ===" -ForegroundColor Cyan
  gh auth status -a
  Write-Host ""
}

Show-Accounts

Write-Host "What do you want to do?" -ForegroundColor Yellow
Write-Host "  1) Use the CURRENT active account and push"
Write-Host "  2) Add / switch to ANOTHER GitHub account (browser login), then push"
Write-Host "  3) Cancel"
Write-Host ""
$choice = Read-Host "Enter 1, 2, or 3"

if ($choice -eq "3" -or [string]::IsNullOrWhiteSpace($choice)) {
  Write-Host "Cancelled."
  exit 0
}

if ($choice -eq "2") {
  Write-Host ""
  Write-Host "A browser window will open. Sign in as the account you want" -ForegroundColor Yellow
  Write-Host "(for example HamzaFarooq3333), then approve access." -ForegroundColor Yellow
  Write-Host ""
  # Device-flow login; user picks account in the browser
  gh auth login --hostname github.com --git-protocol https --web --skip-ssh-key
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Login failed or was cancelled." -ForegroundColor Red
    exit 1
  }
  Write-Host ""
  Write-Host "Login done. Active account:" -ForegroundColor Green
  gh auth status
}

Write-Host ""
$who = gh api user --jq ".login"
Write-Host "Pushing as GitHub user: $who" -ForegroundColor Green

$defaultRepo = "https://github.com/$who/Latest_Gold_Trading_Bot.git"
Write-Host ""
Write-Host "Target repo URL" -ForegroundColor Yellow
Write-Host "  Press Enter for default: $defaultRepo"
$repoUrl = Read-Host "Repo URL"
if ([string]::IsNullOrWhiteSpace($repoUrl)) { $repoUrl = $defaultRepo }
$repoUrl = $repoUrl.Trim()

Write-Host ""
Write-Host "Commit author email" -ForegroundColor Yellow
Write-Host "  1) hamza.farooq@onyxion.io"
Write-Host "  2) bscs23057@itu.edu.pk"
Write-Host "  3) Type a custom email"
$emailChoice = Read-Host "Enter 1, 2, or 3"
switch ($emailChoice) {
  "1" { $authorEmail = "hamza.farooq@onyxion.io"; $authorName = "Hamza Farooq" }
  "2" { $authorEmail = "bscs23057@itu.edu.pk"; $authorName = "Muhammad Hamza Farooq" }
  "3" {
    $authorName = Read-Host "Git name"
    $authorEmail = Read-Host "Git email"
  }
  default {
    Write-Host "Invalid email choice." -ForegroundColor Red
    exit 1
  }
}
if (-not $authorName -or -not $authorEmail -or ($authorEmail -notmatch "@")) {
  Write-Host "Name/email invalid." -ForegroundColor Red
  exit 1
}

Write-Host ""
Write-Host "Author will be: $authorName <$authorEmail>" -ForegroundColor Cyan
Write-Host "Remote URL:     $repoUrl" -ForegroundColor Cyan
$confirm = Read-Host "Type YES to commit (if needed) and push"
if ($confirm -ne "YES") {
  Write-Host "Cancelled."
  exit 0
}

# Ensure pack is staged (bridge, mq5 via Ali PC Deployment, dashboard, rules)
$paths = @(
  "Ali PC Deployment",
  "Google Console Deployment/app",
  "Google Console Deployment/asim-gcp.service",
  "Google Console Deployment/nginx-asim-gcp.conf",
  "Google Console Deployment/install.sh",
  "Google Console Deployment/deploy.ps1",
  "Google Console Deployment/deploy_best_xauusdm_engine.ps1",
  "Google Console Deployment/deploy_ali_gaga_xtrend.ps1",
  "Google Console Deployment/fix_access_and_deploy.ps1",
  "Google Console Deployment/fresh_wipe_before_start.ps1",
  "Google Console Deployment/open_public_firewall_permanent.ps1",
  "Google Console Deployment/OPEN_SITE_PUBLIC_NOW.ps1",
  "Google Console Deployment/start_ali_interactive.ps1",
  "Google Console Deployment/env.instance-20260831-171822",
  "Google Console Deployment/env.bridge.hamzatestserver01",
  "Google Console Deployment/env.hamzatestserver01",
  "Google Console Deployment/.env.example",
  "Google Console Deployment/best_controls.json",
  "Google Console Deployment/README.md",
  "TRADING_RULES.md",
  "github"
)
$existing = @()
foreach ($p in $paths) {
  if (Test-Path (Join-Path $RepoRoot $p)) { $existing += $p }
}
git add -- @existing
git reset HEAD -- "Google Console Deployment/app/suggested_logic_backtest_results.json" 2>$null

$env:GIT_AUTHOR_NAME = $authorName
$env:GIT_AUTHOR_EMAIL = $authorEmail
$env:GIT_COMMITTER_NAME = $authorName
$env:GIT_COMMITTER_EMAIL = $authorEmail

$staged = git diff --cached --name-only
if ($staged) {
  $msg = @"
Publish live pack: Python bridge, MQ5, dashboard, and trading rules.
"@
  git commit -m $msg
  if ($LASTEXITCODE -ne 0) { throw "commit failed" }
  git log -1 --format="Committed as %an <%ae>%n%H %s"
} else {
  Write-Host "Nothing new to commit — pushing current HEAD." -ForegroundColor Yellow
}

$remoteName = "push-target"
$remotes = git remote
if ($remotes -match "(?m)^$remoteName$") {
  git remote set-url $remoteName $repoUrl
} else {
  git remote add $remoteName $repoUrl
}

# Create repo on GitHub if missing (same logged-in user)
$ownerRepo = $null
if ($repoUrl -match "github\.com[:/]([^/]+)/([^/.]+)") {
  $ownerRepo = "$($Matches[1])/$($Matches[2])"
}
if ($ownerRepo) {
  gh repo view $ownerRepo 2>$null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Repo missing — creating $ownerRepo ..." -ForegroundColor Yellow
    gh repo create $ownerRepo --public --description "Onyxion gold trading bot: bridge, MQ5, dashboard, rules"
    if ($LASTEXITCODE -ne 0) {
      Write-Host "Could not create repo. Create it in the browser, then re-run." -ForegroundColor Red
      exit 1
    }
  }
}

Write-Host "Pushing HEAD -> ${remoteName}/main ..." -ForegroundColor Cyan
git push -u $remoteName HEAD:main
if ($LASTEXITCODE -ne 0) { throw "push failed" }

Write-Host ""
Write-Host "Done. Open: $repoUrl" -ForegroundColor Green
if ($ownerRepo) {
  Start-Process "https://github.com/$ownerRepo"
}
