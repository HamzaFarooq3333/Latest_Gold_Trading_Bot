@echo off
REM ===================================================================
REM  CHECK whether data is actually reaching the GCP dashboard.
REM  Shows the Onyxion processes and the live desk heartbeat age.
REM ===================================================================
title Onyxion - CHECK
set "ROOT=C:\onyxion-ali"
set "PY=%ROOT%\venv\Scripts\python.exe"

echo === Onyxion processes ===
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe' OR Name='terminal64.exe'\" | Where-Object { $_.Name -eq 'terminal64.exe' -or $_.CommandLine -like '*onyxion-ali*' } | Select-Object ProcessId,Name,@{n='Script';e={($_.CommandLine -split ' ' | Where-Object { $_ -like '*.py*' } | Select-Object -First 1)}} | Format-Table -AutoSize"

echo === Live desk status ===
"%PY%" "%ROOT%\scripts\check_bridge_live.py"

echo.
echo === Bridge status file (written every heartbeat) ===
type "%ROOT%\state\bridge_status.json" 2>nul
echo.
pause
