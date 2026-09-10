@echo off
REM ===================================================================
REM  CHECK whether data is actually reaching the GCP dashboard.
REM  Shows the required processes and the live heartbeat age.
REM ===================================================================
title Onyxion - CHECK

echo === Required processes ===
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='terminal64.exe'\" | Select-Object ProcessId,Name,CommandLine | Format-Table -AutoSize -Wrap"

echo.
echo === Live desk status ===
"C:\Program Files\Python312\python.exe" -c "import ssl,urllib.request,json;ctx=ssl.create_default_context();ctx.check_hostname=False;ctx.verify_mode=ssl.CERT_NONE;d=json.loads(urllib.request.urlopen(urllib.request.Request('https://35.253.21.246/api/broker/state'),timeout=20,context=ctx).read());b=d['broker'];br=b['bridge'];a=b['account'];age=b['heartbeat_age_sec'];print('heartbeat age :',round(age,1),'sec');print('status        :','LIVE - data flowing' if age<20 else 'STALE - data NOT flowing');print('mt5           :',br['mt5_connection']);print('login/symbol  :',a['login'],b['symbol']);print('lot size      :',br['lot']);print('last tick     :',br['tick_time'])"

echo.
pause
