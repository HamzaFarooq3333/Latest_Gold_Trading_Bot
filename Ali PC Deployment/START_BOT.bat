@echo off
REM ===================================================================
REM  Onyxion - START BOT  (safe to run any time, as often as you like)
REM
REM  1. Checks whether the bridge is genuinely LIVE, meaning the GCP desk
REM     is still receiving heartbeats. A running python.exe is NOT proof:
REM     the bridge has previously sat in a restart loop with a process
REM     present the whole time while no data reached the desk.
REM  2. LIVE      -> prints "BRIDGE IS LIVE" and stops. Nothing restarted.
REM     STARTING  -> bridge is young and still connecting; leaves it alone.
REM     NOT LIVE  -> prints "STARTING" and brings the whole stack up.
REM
REM  Every start step below is idempotent, so nothing is ever duplicated.
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
    echo ============================================================
    echo  BRIDGE IS LIVE - nothing to do.
    echo ============================================================
    goto :end
)

if "!STATE!"=="2" (
    echo ============================================================
    echo  BRIDGE IS STARTING - already coming up, leaving it alone.
    echo  Re-run this file in a minute to confirm it went live.
    echo ============================================================
    goto :end
)

echo ============================================================
echo  STARTING - bridge is not live. Bringing the stack up...
echo ============================================================
echo.

REM --- 1. MT5 terminal (auto-logs in from .env; skips if already running) --
echo [1/4] MetaTrader 5 terminal...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\launch_mt5.ps1" -Root "%ROOT%"
echo.

REM --- 2. Watchdog, which owns bridge_trader.py and restarts it on crash ---
REM     start_bridge_stack.ps1 is singleton-aware: it exits if already up.
echo [2/4] Bridge watchdog...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\start_bridge_stack.ps1" -Profile ali -Root "%ROOT%"
echo.

REM --- 3. Connection monitor: watches MT5 + desk and repairs the stack -----
echo [3/4] Connection monitor...
powershell -NoProfile -Command "$t = Get-ScheduledTask -TaskName 'OnyxionAli-ConnectionMonitor' -ErrorAction SilentlyContinue; if ($t) { if ($t.State -ne 'Running') { Start-ScheduledTask -TaskName 'OnyxionAli-ConnectionMonitor'; 'connection monitor started' } else { 'connection monitor already running' } } else { 'task missing - starting directly'; Start-Process -FilePath '%ROOT%\venv\Scripts\pythonw.exe' -ArgumentList '\"%ROOT%\connection_monitor.py\"' -WorkingDirectory '%ROOT%' -WindowStyle Hidden }"
echo.

REM --- 4. GitHub update agent: candle-safe Latest pull + status to desk -----
echo [4/4] GitHub update agent...
powershell -NoProfile -Command "$t = Get-ScheduledTask -TaskName 'OnyxionAli-GitHubAgent' -ErrorAction SilentlyContinue; if (-not $t) { & '%ROOT%\scripts\register_github_agent_task.ps1' -Root '%ROOT%' | Out-Null; $t = Get-ScheduledTask -TaskName 'OnyxionAli-GitHubAgent' -ErrorAction SilentlyContinue }; if ($t) { Start-ScheduledTask -TaskName 'OnyxionAli-GitHubAgent'; 'github agent started/running' } else { 'task missing - starting directly'; Start-Process -FilePath '%ROOT%\venv\Scripts\pythonw.exe' -ArgumentList '\"%ROOT%\github_update_agent.py\"' -WorkingDirectory '%ROOT%' -WindowStyle Hidden }"
echo.

REM --- give MT5 login + first bar fetch time to produce a heartbeat --------
REM     ping is used as the delay, not "timeout": timeout aborts with
REM     "Input redirection is not supported" whenever stdin is redirected,
REM     which happens when this file is run from a task or a pipe.
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

echo ============================================================
"%PY%" "%ROOT%\scripts\check_bridge_live.py"
set "STATE=!ERRORLEVEL!"
if "!STATE!"=="0" (
    echo  BRIDGE IS LIVE - startup complete.
) else (
    echo  BRIDGE DID NOT GO LIVE YET.
    echo.
    echo  Check, in this order:
    echo    1^) Is Algo Trading enabled in MT5? ^(toolbar button^)
    echo    2^) %ROOT%\logs\bridge_asim.log        - last 20 lines
    echo    3^) %ROOT%\logs\connection_check.log   - MT5 / desk state
    echo    4^) Is this PC's public IP allowed on the GCP firewall?
)
echo ============================================================

:end
echo.
echo Running processes:
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe' OR Name='terminal64.exe'\" | Where-Object { $_.Name -eq 'terminal64.exe' -or $_.CommandLine -like '*onyxion*' } | Select-Object ProcessId, Name | Format-Table -AutoSize"
echo.
echo The bot runs in the background - you can close this window.
pause
endlocal
