# Claude / Cursor setup prompt — Ali PC (MT5 → GCP live)

**Copy everything below the line into Claude or Cursor on Ali's Windows PC.**

---

You are setting up and verifying the **Onyxion live trading bridge** on this Windows PC for profile **ali**.

## Goal

1. **Check** the install (files, venv, `.env`, MT5 terminal, git clone)
2. **Start** the stack (`START_BOT.bat` or the four scheduled tasks)
3. **Verify** MT5 is logged in, the bridge heartbeats reach the desk, and the Ali PC panel on the desk shows all processes running
4. **Keep it running** — everything is supervised by hidden scheduled tasks; nothing needs a console window

This PC = execution / feed. The GCP Linux VM = dashboard. **Never** run `gcloud`, `install.sh` or SSH to GCP from this PC, and never ask for Google passwords.

## Fixed configuration (do not change unless asked)

| Item | Value |
|------|--------|
| Install root | `C:\onyxion-ali` (venv at `C:\onyxion-ali\venv`) |
| GitOps clone | `C:\onyxion-src\Latest_Gold_Trading_Bot` (source of truth: `HamzaFarooq3333/Latest_Gold_Trading_Bot`) |
| Live desk | `https://35.253.21.246/live` · backtest `https://35.253.21.246/` |
| `ASIM_LAB_URL` / `ASIM_LAB_INSECURE` | `https://35.253.21.246` / `1` |
| MT5 | demo `472640728` @ `Exness-MT5Trial16`, `XAUUSDm` **M15**, portable terminal at `C:\onyxion-ali\exness-mt5` |
| `XTREND_SOURCE` | `gaga` |
| Strategy | `ENTRY_EVERY_CANDLE=1`, `HIST_THRESH=10`, `TSL_ATR_MULT=1.25`, `VOLUME=0.02`, `MAXPOS=20`, `MAX_SUPP=0` |
| Hour / weekend filters | `SKIP_WORST_HOURS=0`, `FOCUS_BEST_HOURS=0`, `SKIP_WEEKENDS=0` |
| Password | demo **trading** password, only in `C:\onyxion-ali\.env` — never print it |

`.env` is the source of truth; the desk's Live controls override it only once saved on the dashboard.

## Read first

`START_HERE.md`, `CONNECT_LIVE.md`, `..\TRADING_RULES.md`, `mq5\CHART_SETUP.md`.

---

# PHASE A — Check

```powershell
Test-Path C:\onyxion-ali\venv\Scripts\python.exe
C:\onyxion-ali\venv\Scripts\python.exe -c "import MetaTrader5, numpy; print('ok')"
Test-Path C:\onyxion-ali\exness-mt5\terminal64.exe
Test-Path C:\onyxion-src\Latest_Gold_Trading_Bot\.git
Get-ScheduledTask -TaskName 'OnyxionAli-*' | Select-Object TaskName, State
```

All must exist; the four tasks are `OnyxionAli-MT5`, `OnyxionAli-BridgeStack`, `OnyxionAli-ConnectionMonitor`, `OnyxionAli-GitHubAgent`.
If anything is missing run, from the pack folder: `powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1`.

Confirm `C:\onyxion-ali\.env` carries the fixed configuration above (do not print the password line).

# PHASE B — Start

```powershell
C:\onyxion-ali\venv\Scripts\python.exe C:\onyxion-ali\healthcheck_mt5.py --env C:\onyxion-ali\.env
```
Must print `PASS`. Then double-click `C:\onyxion-ali\START_BOT.bat` (or `Get-ScheduledTask 'OnyxionAli-*' | Start-ScheduledTask`).

# PHASE C — Verify

```powershell
powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\verify_live_connection.ps1
C:\onyxion-ali\venv\Scripts\python.exe C:\onyxion-ali\scripts\check_bridge_live.py
```

Then in the browser at `https://35.253.21.246/live`: `BRIDGE LIVE`, login `472640728`, `TRADING MODE ON`, and the **Ali PC** panel shows all processes ON and GitHub sync `IN SYNC`.

# Safety rules (hard)

- Demo only — the bridge refuses a real account.
- Never paste `.env` passwords anywhere.
- Never flatten / close positions for a code update (the GitHub agent is candle-safe).
- Never change `XTREND_SOURCE`, the hour filters or the strategy keys without being asked.

# Final report format (no passwords)

1. Phase A: each check PASS/FAIL
2. healthcheck: PASS/FAIL + reason
3. processes running (MT5, watchdog, bridge, connection monitor, GitHub agent): yes/no + PIDs
4. live desk: BRIDGE LIVE yes/no + heartbeat age
5. Ali PC panel on the desk: all ON yes/no, GitHub sync state
6. this PC's public IP (`https://ifconfig.me`) if the desk is unreachable
7. blockers remaining

---

**End of prompt**
