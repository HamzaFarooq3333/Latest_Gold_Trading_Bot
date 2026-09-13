@echo off
REM ===================================================================
REM  Onyxion - START BOT  (safe to run any time, as often as you like)
REM
REM  Brings up and verifies the whole Ali PC stack:
REM    - MT5 terminal + bridge (watchdog -> bridge_trader.py)
REM    - connection monitor   (5-min MT5/desk health checks + repairs)
REM    - GitHub update agent  (candle-safe pull of the Latest repo)
REM
REM  Every step is idempotent: a part that is already running is left
REM  alone, only missing parts are started. At the end it checks all
REM  three and prints either
REM      EVERYTHING IS NOW LIVE AND RUNNING
REM  or the list of what is still down and where to look.
REM
REM  "Live" for the bridge means the GCP desk is receiving heartbeats,
REM  not merely that a python.exe exists (the bridge has previously sat
REM  in a restart loop with a process present the whole time).
REM ===================================================================
setlocal EnableDelayedExpansion
title Onyxion - START

set "ROOT=C:\onyxion-ali"
set "MT5_ROOT=%ROOT%"

REM --- pick the interpreter: the venv holds MetaTrader5, so prefer it -----
set "PY=%ROOT%\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Program Files\Python312\python.exe"
if not exist "%PY%" (
    echo [ERROR] No Python found. Looked for:
    echo         %ROOT%\venv\Scripts\python.exe
    echo         C:\Program Files\Python312\python.exe
    goto :end
)

echo ============================================================
echo  Checking bridge status...
echo ============================================================
"%PY%" "%ROOT%\scripts\check_bridge_live.py"
set "STATE=!ERRORLEVEL!"
echo.

if "!STATE!"=="0" (
    echo  Bridge is already LIVE - not restarting it.
    goto :pipeline
)
if "!STATE!"=="2" (
    echo  Bridge is STARTING - already coming up, leaving it alone.
    goto :pipeline
)

echo ============================================================
echo  Bridge is not live. Bringing it up...
echo ============================================================
echo.

REM --- 1. MT5 terminal (auto-logs in from .env; skips if already running) --
echo [1/2] MetaTrader 5 terminal...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\launch_mt5.ps1" -Root "%ROOT%"
echo.

REM --- 2. Watchdog, which owns bridge_trader.py and restarts it on crash ---
REM     start_bridge_stack.ps1 is singleton-aware: it exits if already up.
echo [2/2] Bridge watchdog...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\start_bridge_stack.ps1" -Profile ali -Root "%ROOT%"
echo.

:pipeline
REM ===================================================================
REM  GitHub pipeline + monitor: always ensured, even when the bridge was
REM  already live (the old START only did this on a cold start, so a
REM  dead agent stayed dead until the next logon).
REM  The scheduled tasks are registered on first use; Start-ScheduledTask
REM  is a no-op when the task's process is already running
REM  (MultipleInstances = IgnoreNew).
REM ===================================================================
echo ============================================================
echo  Ensuring connection monitor + GitHub update agent...
echo ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = '%ROOT%';" ^
  "$need = @('OnyxionAli-ConnectionMonitor', 'OnyxionAli-GitHubAgent');" ^
  "$missing = $need | Where-Object { -not (Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue) };" ^
  "if ($missing) { Write-Host 'registering scheduled tasks...'; & (Join-Path $root 'scripts\register_tasks.ps1') -Root $root | Out-Null };" ^
  "foreach ($name in $need) {" ^
  "  $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue;" ^
  "  if ($t) { if ($t.State -eq 'Running') { Write-Host ('  ' + $name + ': already running') } else { Start-ScheduledTask -TaskName $name; Write-Host ('  ' + $name + ': started') } }" ^
  "  else {" ^
  "    $script = if ($name -like '*Monitor*') { 'connection_monitor.py' } else { 'github_update_agent.py' };" ^
  "    Write-Host ('  ' + $name + ': task missing - starting ' + $script + ' directly');" ^
  "    Start-Process -FilePath (Join-Path $root 'venv\Scripts\pythonw.exe') -ArgumentList ('\"' + (Join-Path $root $script) + '\"') -WorkingDirectory $root -WindowStyle Hidden" ^
  "  }" ^
  "}"
echo.

REM --- give MT5 login + first bar fetch time to produce a heartbeat --------
REM     ping is used as the delay, not "timeout": timeout aborts with
REM     "Input redirection is not supported" whenever stdin is redirected,
REM     which happens when this file is run from a task or a pipe.
if "!STATE!"=="0" goto :verify
echo Waiting up to 90s for the first heartbeat...
set "OK="
for /L %%i in (1,1,9) do (
    if not defined OK (
        ping -n 11 127.0.0.1 >nul 2>&1
        "%PY%" "%ROOT%\scripts\check_bridge_live.py" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "OK=1"
            echo    heartbeat detected after %%i0s
        )
    )
)
echo.

:verify
REM ===================================================================
REM  Final verification of all three parts.
REM    bridge   = desk heartbeat fresh (check_bridge_live.py exit 0)
REM    monitor  = connection_monitor.py process under %ROOT%
REM    pipeline = github_update_agent.py process under %ROOT%
REM  The agent/monitor are started hidden a moment ago, so give them a
REM  few seconds before looking for the process.
REM ===================================================================
ping -n 4 127.0.0.1 >nul 2>&1
echo ============================================================
echo  Verifying...
echo ============================================================
"%PY%" "%ROOT%\scripts\check_bridge_live.py"
set "BRIDGE=!ERRORLEVEL!"

set "AGENT=1"
set "MONITOR=1"
REM  The probe writes "<agent> <monitor>" (1/0) to a temp file; reading it
REM  back with for /f avoids cmd mangling the '=' inside a backquoted
REM  PowerShell command.
set "PROBE=%TEMP%\onyxion_stack_probe.txt"
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*onyxion-ali*' }; $a = [int][bool]($p | Where-Object { $_.CommandLine -like '*github_update_agent.py*' }); $m = [int][bool]($p | Where-Object { $_.CommandLine -like '*connection_monitor.py*' }); Set-Content -Path '%PROBE%' -Value \"$a $m\" -Encoding ASCII"
for /f "usebackq tokens=1,2" %%a in ("%PROBE%") do (
    if "%%a"=="1" set "AGENT=0"
    if "%%b"=="1" set "MONITOR=0"
)
del "%PROBE%" >nul 2>&1

if "!AGENT!"=="0" (echo  GitHub update agent : RUNNING) else (echo  GitHub update agent : NOT RUNNING)
if "!MONITOR!"=="0" (echo  Connection monitor  : RUNNING) else (echo  Connection monitor  : NOT RUNNING)
if "!BRIDGE!"=="0" (echo  Bridge -^> GCP desk  : LIVE) else (echo  Bridge -^> GCP desk  : NOT LIVE)
echo.

echo ============================================================
if "!BRIDGE!"=="0" if "!AGENT!"=="0" if "!MONITOR!"=="0" (
    echo  EVERYTHING IS NOW LIVE AND RUNNING
    echo ============================================================
    goto :end
)
echo  NOT EVERYTHING IS UP YET - see the lines above.
echo.
if not "!BRIDGE!"=="0" (
    echo  Bridge checks, in this order:
    echo    1^) Is Algo Trading enabled in MT5? ^(toolbar button^)
    echo    2^) %ROOT%\logs\bridge_YYYYMMDD.log    - last 20 lines
    echo    3^) %ROOT%\logs\connection_check.log   - MT5 / desk state
    echo    4^) Is this PC's public IP allowed on the GCP firewall?
    echo    5^) If it says STARTING, re-run this file in a minute.
)
if not "!AGENT!"=="0" (
    echo  GitHub agent: %ROOT%\logs\github_update_agent.log
)
if not "!MONITOR!"=="0" (
    echo  Connection monitor: %ROOT%\logs\connection_check.log
)
echo ============================================================

:end
echo.
echo Running processes (each script shows twice: venv launcher + its interpreter):
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe' OR Name='terminal64.exe'\" | Where-Object { $_.Name -eq 'terminal64.exe' -or $_.CommandLine -like '*onyxion*' } | Select-Object ProcessId, Name, @{n='Script';e={($_.CommandLine -split ' ' | Where-Object { $_ -like '*.py*' } | Select-Object -First 1)}} | Format-Table -AutoSize"
echo.
echo The bot runs in the background - you can close this window.
pause
endlocal
