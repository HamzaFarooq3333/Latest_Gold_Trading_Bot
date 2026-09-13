# Ali PC Deployment — START HERE

**What this pack is:** everything Ali needs on a **Windows PC** to run MetaTrader 5, the Onyxion bridge, and push live bars / trades / heartbeats to the **Ali GCP desk**.

| Role | URL |
|------|-----|
| Live trading dashboard | https://35.253.21.246/live |
| Backtest UI | https://35.253.21.246/ |

VM `instance-20260831-171822` (us-central1-a). If the VM is restarted the external IP can change — update `ASIM_LAB_URL` in `C:\onyxion-ali\.env`.

Full connect checklist: **`CONNECT_LIVE.md`**.

---

## 5-minute outline

1. Copy this **`Ali PC Deployment`** folder to the PC (or clone the repo to `C:\onyxion-src\Latest_Gold_Trading_Bot`).
2. Install **Python 3.11+ 64-bit** and **Git**.
3. In PowerShell:

```powershell
cd "PATH\TO\Ali PC Deployment"
powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1
```

4. Install the portable Exness MT5 to `C:\onyxion-ali\exness-mt5`, log in demo **472640728** / **Exness-MT5Trial16**, enable Algo Trading, open XAUUSDm **M15**.
5. Compile the MQ5 indicators (`mq5\CHART_SETUP.md`) — optional, for the chart only.
6. Fill `ASIM_MT5_PASSWORD` in `C:\onyxion-ali\.env`, then double-click `C:\onyxion-ali\START_BOT.bat`.
7. Confirm https://35.253.21.246/live shows MT5 ONLINE and the Ali PC panel shows all processes running.

---

## Engine rules on this pack

See `../TRADING_RULES.md`. Short version: every green/red candle that breaks the previous HA body and sits clear of X-Trend opens a ticket (stacking); each ticket trails a 1.25 × ATR(14) stop to every close; amber flattens.

---

## Folder map

```
Ali PC Deployment/
  INSTALL.ps1                 one-shot installer / upgrader (venv, tasks, files)
  START_BOT.bat / CHECK_BOT.bat / STOP_BOT.bat
  fix_script_encoding.ps1     UTF-8 BOM repair for PowerShell 5.1
  env/.env.ali.example        config template (source of truth once copied to C:\onyxion-ali\.env)
  runtime/                    bridge, engine, watchdog, monitor, GitHub agent, safety gate, tests
  scripts/                    start / launch / update / verify / task registration
  mq5/                        Histogram + KJ X-Trend (Gaga) indicators for the chart
  *.html                      backtest and live-trade reports
```

Live code lives in **`C:\onyxion-ali\`**; the GitHub agent keeps it in sync with `main` (candle-safe, never flattens).

---

## Safety

- Demo account only (the bridge refuses a real account).
- Never commit `.env`; never paste passwords in chat.
- Updates never touch `.env` or `state\` and never close positions.
