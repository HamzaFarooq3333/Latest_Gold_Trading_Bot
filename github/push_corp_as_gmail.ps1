#Requires -Version 5.1
# Login, then push bridge + MQ5 + dashboard + TRADING_RULES to Onyxion-Corp/Gold_Trading_Bot
# Author email: hamzafarooqsea@gmail.com

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

$TargetUrl = "https://github.com/Onyxion-Corp/Gold_Trading_Bot.git"
$AuthorName = "Hamza Farooq"
$AuthorEmail = "hamzafarooqsea@gmail.com"

Write-Host "GitHub CLI:" -ForegroundColor Cyan
gh --version
Write-Host ""
Write-Host "CURRENT account:" -ForegroundColor Yellow
gh auth status
Write-Host ""
Write-Host "Sign in with the GitHub account for: $AuthorEmail" -ForegroundColor Yellow
Write-Host "that can access Onyxion-Corp." -ForegroundColor Yellow
Write-Host ""
$go = Read-Host "Type YES to open browser login now"
if ($go -ne "YES") {
  Write-Host "Cancelled."
  Write-Host "Press Enter to close..."
  [void][System.Console]::ReadLine()
  exit 0
}

Write-Host ""
Write-Host "In the browser: sign in as $AuthorEmail, then Approve." -ForegroundColor Cyan
gh auth login --hostname github.com --git-protocol https --web --skip-ssh-key
if ($LASTEXITCODE -ne 0) {
  Write-Host "gh auth login failed" -ForegroundColor Red
  Write-Host "Press Enter to close..."
  [void][System.Console]::ReadLine()
  exit 1
}

Write-Host ""
Write-Host "Active account after login:" -ForegroundColor Green
gh auth status
$who = gh api user --jq ".login"
Write-Host "Logged in as: $who" -ForegroundColor Green

$remote = "onyxion-corp"
$remotes = @(git remote)
if ($remotes -contains $remote) {
  git remote set-url $remote $TargetUrl
} else {
  git remote add $remote $TargetUrl
}

gh repo view Onyxion-Corp/Gold_Trading_Bot 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Repo missing - creating Onyxion-Corp/Gold_Trading_Bot ..." -ForegroundColor Yellow
  gh repo create Onyxion-Corp/Gold_Trading_Bot --private --description "Onyxion gold trading bot: bridge, MQ5, dashboard, rules"
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Could not create under Onyxion-Corp. Create it as org admin, then press Enter." -ForegroundColor Red
    Read-Host "Press Enter after the empty repo exists"
  }
}

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
git reset HEAD -- "Google Console Deployment/app/suggested_logic_backtest_results.json" 2>$null | Out-Null

$env:GIT_AUTHOR_NAME = $AuthorName
$env:GIT_AUTHOR_EMAIL = $AuthorEmail
$env:GIT_COMMITTER_NAME = $AuthorName
$env:GIT_COMMITTER_EMAIL = $AuthorEmail

$staged = git diff --cached --name-only
if ($staged) {
  git commit -m "Publish live pack: Python bridge, MQ5, dashboard, and trading rules."
  if ($LASTEXITCODE -ne 0) {
    Write-Host "commit failed" -ForegroundColor Red
    Write-Host "Press Enter to close..."
    [void][System.Console]::ReadLine()
    exit 1
  }
}

$hash = git log -1 --format="%h"
$an = git log -1 --format="%an"
$ae = git log -1 --format="%ae"
$subj = git log -1 --format="%s"
Write-Host "About to push: $hash"
Write-Host "Author: $an <$ae>"
Write-Host $subj
Write-Host ""
$confirm = Read-Host "Type YES to push to $TargetUrl"
if ($confirm -ne "YES") {
  Write-Host "Cancelled."
  Write-Host "Press Enter to close..."
  [void][System.Console]::ReadLine()
  exit 0
}

git push -u $remote HEAD:main
if ($LASTEXITCODE -ne 0) {
  Write-Host "push failed" -ForegroundColor Red
  Write-Host "Press Enter to close..."
  [void][System.Console]::ReadLine()
  exit 1
}

Write-Host "Done: https://github.com/Onyxion-Corp/Gold_Trading_Bot" -ForegroundColor Green
Start-Process "https://github.com/Onyxion-Corp/Gold_Trading_Bot"
Write-Host ""
Write-Host "Press Enter to close this window..." -ForegroundColor Yellow
[void][System.Console]::ReadLine()
