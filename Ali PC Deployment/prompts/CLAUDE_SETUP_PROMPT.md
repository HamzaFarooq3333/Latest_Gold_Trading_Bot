# Claude / Cursor setup prompt — Ali PC (MT5 → GCP live)

**Copy everything below the line into Claude or Cursor on Ali’s Windows PC.**  
Keep this folder as **`Ali PC Deployment`**. Do not invent AWS URLs or Hamza IPs.

---

You are setting up and verifying the **Onyxion live trading bridge** on this Windows PC for profile **ali**.

## Goal

1. **First check** that all required software / files / env / MQ5 are installed correctly  
2. **Then start** services one by one (MT5 → healthcheck → Python watchdog/bridge)  
3. **Verify** each layer is up before starting the next  
4. **Confirm** the live GCP desk is connected and receiving heartbeats  
5. **Keep Python running** (watchdog must stay alive; do not exit until proven)

This PC = execution / feed. GCP Linux VM = dashboard + decision desk.

## Fixed configuration (do not change unless asked)

| Item | Value |
|------|--------|
| Install root | `C:\onyxion-ali` |
| Profile | `ali` |
| GCP live URL | `https://35.253.21.246/live` |
| GCP backtest URL | `https://35.253.21.246/` |
| Browser login | `ali` / `123451` |
| Bridge update zip | `https://35.253.21.246/bridge/onyxion-bridge-bundle.zip` |
| `ASIM_LAB_URL` | `https://35.253.21.246` |
| `ASIM_LAB_INSECURE` | `1` |
| MT5 login | `472640728` |
| MT5 server | `Exness-MT5Trial16` |
| Symbol / period | `XAUUSDm` **M15** |
| `XTREND_SOURCE` | `gaga` (KJ GagaTrend) |
| Hour filters | **`SKIP_WORST_HOURS=0`** and **`FOCUS_BEST_HOURS=0`** (must stay OFF) |
| Chart clock | **UTC** on MT5 and on `/live` (must match) |
| Password | from `env\.env.ali.example` (demo **trading** password, not investor) |

**Do NOT use** old IPs `35.223.235.204`, `35.232.76.12`, or any AWS Lambda URL.

## Read first (in this pack)

1. `START_HERE.md`
2. `CONNECT_LIVE.md`
3. `mq5\CHART_SETUP.md` (Histogram + KJ X-Trend placement)
4. `env\.env.ali.example`
5. `docs\TRADING_RULES.md` (do not change strategy rules unless asked)

---

# PHASE A — Check installations (do this BEFORE starting anything)

Run each check. **STOP and fix** any FAIL before Phase B.

### A1. OS / Python

```powershell
python --version
python -c "import struct; print(struct.calcsize('P')*8)"
```

Expect: Python **3.11 or 3.12**, print **`64`**.

```powershell
python -c "import MetaTrader5, numpy; print('ok')"
```

Expect: `ok`. If ImportError → `pip install -r C:\onyxion-ali\requirements.txt` (or run INSTALL.ps1).

### A2. Pack + install root

Confirm these exist:

- This pack folder: `Ali PC Deployment\` with `INSTALL.ps1`, `runtime\`, `mq5\`, `env\.env.ali.example`
- After install: `C:\onyxion-ali\bridge_trader.py`, `mt5_live_engine.py`, `watchdog_exness.py`, `healthcheck_mt5.py`, `.env`, `mq5\`

If `C:\onyxion-ali` missing:

```powershell
cd "FULL_PATH_TO\Ali PC Deployment"
powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1 -InstallMt5
```

### A3. `.env` critical keys

Open `C:\onyxion-ali\.env` and confirm **exactly**:

```text
ASIM_MT5_LOGIN=472640728
ASIM_MT5_SERVER=Exness-MT5Trial16
ASIM_MT5_SYMBOL=XAUUSDm
ASIM_MT5_TERMINAL_PATH=C:\onyxion-ali\exness-mt5\terminal64.exe
ASIM_MT5_PORTABLE=1
ASIM_LAB_URL=https://35.253.21.246
ASIM_LAB_INSECURE=1
BRIDGE_UPDATE_URL=https://35.253.21.246/bridge/onyxion-bridge-bundle.zip
BRIDGE_PROFILE=ali
XTREND_SOURCE=gaga
XTREND_GATE=1
XTREND_GATE_SUPP=0
TRAIL_ENTRY_BAR=1
TRAIL_EVERY_CANDLE=1
ENTRY_BAR_MODE=defer
SKIP_WORST_HOURS=0
FOCUS_BEST_HOURS=0
SKIP_WEEKENDS=0
```

If `SKIP_WORST_HOURS` or `FOCUS_BEST_HOURS` is `1` → set both to `0` and save.

### A4. MT5 terminal binary

```powershell
Test-Path C:\onyxion-ali\exness-mt5\terminal64.exe
```

Must be `$true`. If not, re-run INSTALL with `-InstallMt5`.

### A5. MQ5 files in the right place

Required sources in pack / install root `mq5\`:

- `OnyxionHistogram.mq5` → Indicators  
- `OnyxionXTrendProxy.mq5` → Indicators (**KJ Gaga / X-Trend**)  
- Experts optional: ValuePublisher, LabBridge, DemoBacktest  

Install into terminal:

```powershell
powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\install_mq5_to_terminal.ps1 -Profile ali -Root C:\onyxion-ali
```

Confirm files exist:

```powershell
Test-Path C:\onyxion-ali\exness-mt5\MQL5\Indicators\OnyxionHistogram.mq5
Test-Path C:\onyxion-ali\exness-mt5\MQL5\Indicators\OnyxionXTrendProxy.mq5
```

Follow `mq5\CHART_SETUP.md`: compile F7, attach Histogram (subwindow) + XTrendProxy (main chart) on **XAUUSDm M15**.

### A6. GCP reachability (firewall)

```powershell
try {
  $u = "https://35.253.21.246/bridge/onyxion-bridge-bundle.zip"
  $r = [System.Net.HttpWebRequest]::Create($u)
  $r.Method = "HEAD"; $r.Timeout = 15000
  $r.ServerCertificateValidationCallback = { $true }
  $resp = $r.GetResponse(); "OK $($resp.StatusCode)"; $resp.Close()
} catch { "FAIL: $($_.Exception.Message)"; "Public IP:"; (Invoke-RestMethod https://ifconfig.me) }
```

If FAIL → report public IP; do **not** start the bridge until the desk allows this `/32`.

### A7. Installation checklist report

Before starting, write: A1–A6 each PASS/FAIL. All must be PASS.

---

# PHASE B — Start one by one (only after Phase A PASS)

### B1. Start MetaTrader 5

1. Launch `C:\onyxion-ali\exness-mt5\terminal64.exe`
2. Login: `472640728` / trading password from `.env` / **Exness-MT5Trial16**
3. Market Watch → **XAUUSDm**
4. Chart **XAUUSDm M15** with Histogram + X-Trend Gaga attached (see CHART_SETUP.md)
5. **Algo Trading** = ON (green)

Wait until account shows balance/equity and chart ticks. Do not proceed if login fails.

### B2. Healthcheck (gate — must PASS)

```powershell
cd C:\onyxion-ali
python healthcheck_mt5.py --env C:\onyxion-ali\.env --model ASIM
```

**STOP if FAIL.** Fix MT5 login / symbol / 64-bit Python / portable path. Do not start bridge yet.

### B3. Start Python watchdog (keeps bridge alive)

```powershell
powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\start_bridge_stack.ps1 -Profile ali -Root C:\onyxion-ali
```

Confirm a python process is running the watchdog:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match "watchdog_exness" } |
  Select-Object ProcessId, CommandLine
```

Expect at least one PID. Logs: `C:\onyxion-ali\logs\`

**Keep this Python process running.** Do not kill it. Do not close the PowerShell and then kill child processes. Leave MT5 open.

---

# PHASE C — Verify everything is up

### C1. Local process

- MT5 terminal open and logged in  
- Watchdog/python running  
- Recent lines in `C:\onyxion-ali\logs\` without repeated fatal errors  

### C2. Optional local verify script

```powershell
powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\verify_live_connection.ps1 -Profile ali -Root C:\onyxion-ali
```

(If script missing, use healthcheck + process check + browser in C3.)

### C3. Live GCP connected

1. Browser → `https://35.253.21.246/live`  
2. Login `ali` / `123451` (accept self-signed cert once)  
3. Confirm all of:
   - Bridge / feed pill **LIVE** (heartbeat age under ~20s)  
   - Login **472640728**, symbol **XAUUSDm**  
   - Candles updating on M15  
   - Chart axis / bar times in **UTC** matching MT5 M15 bar opens (same `:00/:15/:30/:45`)  
   - Feed / tick timestamps advancing each refresh  

4. Also open `https://35.253.21.246/` — backtest UI loads with same login.

If HTTPS times out → firewall; report `https://ifconfig.me`.

### C4. Auto-update task

```powershell
Get-ScheduledTask -TaskName "OnyxionBridgeAutoUpdate-ali"
```

Should be Ready/Running. Optional force pull:

```powershell
powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\auto_update_bridge.ps1 -Profile ali -Root C:\onyxion-ali -Force
```

After force update, re-run **B3** if watchdog stopped.

---

# PHASE D — Keep Python running (persistence)

- Leave **watchdog** running continuously — it restarts `bridge_trader` if it crashes.  
- Leave **MT5** open and logged in.  
- Prefer Windows session logged in; on RDP **disconnect** instead of Sign out.  
- After reboot: MT5 login → healthcheck → `start_bridge_stack.ps1` again.  
- Do **not** start a second bridge instance for the same account.  
- Confirm after 2–3 minutes that `/live` still shows LIVE heartbeats.

---

## Safety rules (hard)

- Demo only — refuse real-money credentials  
- Never paste full `.env` passwords into git/public chat  
- Do not use `--force-action` unless Onyxion asks  
- Do not change `XTREND_SOURCE` away from `gaga`  
- Do not enable best/worst hour filters (`SKIP_WORST_HOURS` / `FOCUS_BEST_HOURS` stay `0`)  
- Do not point `ASIM_LAB_URL` at Hamza or AWS  

## Strategy awareness (do not modify code for this)

Primary = previous-body break + XT clear; SUPP = raw last-trade wick; `TRAIL_ENTRY_BAR=1`. Details in `docs\TRADING_RULES.md`.

---

## Final report format (no passwords)

Reply with only:

1. Phase A checks: each A1–A6 PASS/FAIL  
2. `INSTALL.ps1` run this session: yes/no + result  
3. healthcheck: PASS/FAIL + reason  
4. MT5: last 4 digits of login + server + symbol + M15 chart indicators attached: yes/no  
5. Python watchdog running: yes/no + PID  
6. live dashboard ONLINE: yes/no + last feed/heartbeat UTC time if visible  
7. chart time matches MT5 (UTC): yes/no  
8. hour filters both OFF in `.env`: yes/no  
9. backtest UI loads: yes/no  
10. auto-update task: yes/no  
11. this PC public IP  
12. blockers remaining  

---

**End of prompt**
