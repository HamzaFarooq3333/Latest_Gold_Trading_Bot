@echo off
REM One-shot: bring GitHub agent back online on Ali PC (FamilyHP).
REM Safe while trading — does not flatten or stop the bridge.
set "ROOT=C:\onyxion-ali"
echo Ending stuck GitHub agent task (if any)...
schtasks /End /TN OnyxionAli-GitHubAgent >nul 2>&1
if exist "%ROOT%\state\github_update_agent.lock" del /f /q "%ROOT%\state\github_update_agent.lock"
echo Starting OnyxionAli-GitHubAgent...
schtasks /Run /TN OnyxionAli-GitHubAgent
if errorlevel 1 (
  echo Task missing — falling back to START_BOT.bat
  call "%ROOT%\START_BOT.bat"
) else (
  echo OK — agent task started. Desk Ali PC panel should go ONLINE within ~30s.
)
pause
