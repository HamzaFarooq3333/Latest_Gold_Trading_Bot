@echo off
REM ===================================================================
REM  STOP the Onyxion data flow.
REM  Stops watchdog first so it cannot restart the bridge underneath us.
REM  MT5 itself is left open - close it manually if you want it shut.
REM ===================================================================
title Onyxion - STOP

echo Stopping watchdog, bridge and connection monitor...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'watchdog_exness' } | ForEach-Object { Write-Host ('stopping watchdog ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"
powershell -NoProfile -Command "Start-Sleep -Seconds 3; Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'bridge_trader|connection_monitor' } | ForEach-Object { Write-Host ('stopping ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"

echo.
echo === Remaining processes ===
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='terminal64.exe'\" | Select-Object ProcessId,Name | Format-Table -AutoSize"

echo.
echo NOTE: the scheduled tasks will restart everything within 5 minutes.
echo To keep it stopped, disable them in Task Scheduler (OnyxionAli-*).
pause
