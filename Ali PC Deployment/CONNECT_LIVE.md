# Connect Ali PC (MT5) → GCP live desk

The PC runs MetaTrader 5 and the bridge; the Linux VM hosts the dashboard. Nothing on the PC ever redeploys the VM.

## Link

| Role | Value |
|------|-------|
| Live dashboard | https://35.253.21.246/live |
| Backtest UI | https://35.253.21.246/ |
| GCP VM | `instance-20260831-171822` (us-central1-a) |
| Bridge endpoints | `/api/broker/heartbeat`, `/api/broker/execution`, `/api/broker/controls`, `/api/broker/ali_pc_status` |

If the VM is restarted the external IP can change: update `ASIM_LAB_URL` in `C:\onyxion-ali\.env`.

## What must match

| Setting | Value |
|---------|-------|
| MT5 login / server | `472640728` / `Exness-MT5Trial16` |
| Symbol | `XAUUSDm` |
| `XTREND_SOURCE` | `gaga` |
| `ENTRY_EVERY_CANDLE` / `HIST_THRESH` / `TSL_ATR_MULT` | `0` / `10` (colour ±10) / `1.25` |
| `VOLUME` / `MAXPOS` / `MAX_SUPP` | `0.02` / `20` / `0` |
| `SKIP_WORST_HOURS` / `FOCUS_BEST_HOURS` / `SKIP_WEEKENDS` | `0` / `0` / `0` |
| `ASIM_LAB_INSECURE` | `1` (self-signed HTTPS on the desk) |
| Clock | UTC on MT5 (Exness) and on the desk chart |

## Checklist

1. **Firewall** — this PC's public IP (`https://ifconfig.me`) must be allowed on GCP rule `allow-asim-live-443`.
2. **Install** — `INSTALL.ps1` (see `START_HERE.md`).
3. **Healthcheck**
   ```powershell
   C:\onyxion-ali\venv\Scripts\python.exe C:\onyxion-ali\healthcheck_mt5.py --env C:\onyxion-ali\.env
   ```
4. **Start** — `C:\onyxion-ali\START_BOT.bat` (or `Get-ScheduledTask 'OnyxionAli-*' | Start-ScheduledTask`).
5. **Verify**
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\verify_live_connection.ps1
   ```
6. **Prove the link** — `/live` shows `BRIDGE LIVE`, login `472640728`, `TRADING MODE ON`, candles advancing; `CHECK_BOT.bat` prints `LIVE heartbeat Ns ago`.

## How the link works

```
Ali PC MT5 (XAUUSDm M15)
        │  closed bars + live tick
        ▼
bridge_trader.py + mt5_live_engine.py      state\asim_mt5_engine_state.json
        │  HTTPS POST heartbeat / execution / status every 2 s
        ▼
https://35.253.21.246  (FastAPI /opt/asim-gcp)
        ├── /live   live desk
        └── /       lab + backtest UI (same rules engine)
```

## Logs on the PC (`C:\onyxion-ali\logs\`)

| File | What |
|------|------|
| `bridge_YYYYMMDD.log` | every bar decision and order (14-day retention) |
| `bridge_asim_stderr.log` | crash tracebacks only |
| `connection_check.log` / `data_heartbeat.log` / `errors.log` | connection monitor |
| `github_update_agent.log` / `auto_update.log` / `update_history.log` | GitOps |

## Updating the code

Push to `main` of `HamzaFarooq3333/Latest_Gold_Trading_Bot`. The GitHub agent on the PC detects it within ~90 s, waits for the current M15 candle to close, runs the safety gate, copies the runtime and restarts the bridge; the first candle after the restart takes no new entries. Open tickets and their stops are preserved.
