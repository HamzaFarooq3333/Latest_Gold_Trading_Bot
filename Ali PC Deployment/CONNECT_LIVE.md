# Connect Ali PC (MT5) → GCP live desk

This pack runs **MetaTrader 5 on Ali’s Windows PC** and posts bars / deals / heartbeats to the **Ali GCP desk**. The Linux VM is the dashboard + decision host; this PC is the execution / feed machine.

## Live link (current)

| Role | URL |
|------|-----|
| Live dashboard | https://35.253.21.246/live |
| Backtest UI | https://35.253.21.246/ |
| Bridge auto-update zip | https://35.253.21.246/bridge/onyxion-bridge-bundle.zip |
| Browser login | `ali` / `123451` |
| GCP VM | `instance-20260831-171822` (us-central1-a) |

If the VM is restarted, the external IP can change. Update **both** `ASIM_LAB_URL` and `BRIDGE_UPDATE_URL` in `C:\onyxion-ali\.env`.

## What must match

| Setting | Value |
|---------|--------|
| MT5 login | `472640728` |
| MT5 server | `Exness-MT5Trial16` |
| Symbol | `XAUUSDm` |
| `XTREND_SOURCE` | `gaga` |
| `XTREND_GATE` | `1` (primary) |
| `XTREND_GATE_SUPP` | `0` (SUPP = raw wick only) |
| `TRAIL_ENTRY_BAR` | `1` |
| `ENTRY_BAR_MODE` | `defer` |
| `SKIP_WORST_HOURS` | `0` (OFF) |
| `FOCUS_BEST_HOURS` | `0` (OFF) |
| Chart / feed time | **UTC** (MT5 Exness bar open = live desk axis) |
| `ASIM_LAB_INSECURE` | `1` (desk uses self-signed HTTPS) |

## Connect checklist

1. **Firewall** — this PC’s public IP (`https://ifconfig.me`) must be on GCP rule `allow-asim-live-443` as `/32`.
2. **Install pack**
   ```powershell
   cd "...\Ali PC Deployment"
   powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1 -InstallMt5
   ```
3. **Confirm `.env`** points at `https://35.253.21.246` (see `env\.env.ali.example`).
4. **MT5** — login demo, Algo Trading ON, XAUUSDm **M15**, compile/attach Histogram + XTrendProxy (KJ Gaga). See `mq5\CHART_SETUP.md`.
5. **Healthcheck**
   ```powershell
   python C:\onyxion-ali\healthcheck_mt5.py --env C:\onyxion-ali\.env --model ASIM
   ```
6. **Start bridge** (keep Python/watchdog running)
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\start_bridge_stack.ps1 -Profile ali
   ```
7. **Verify**
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\verify_live_connection.ps1 -Profile ali
   ```
8. **Prove the link**
   - Browser → https://35.253.21.246/live → login `ali` / `123451`
   - Heartbeats advancing, MT5 account `472640728`, symbol `XAUUSDm`
   - Chart candles + axis in **UTC** matching MT5 M15; feed timestamps updating each heartbeat
   - Bridge logs under `C:\onyxion-ali\logs\`

## How the link works

```
Ali PC MT5 (XAUUSDm M15)
        │
        ▼
bridge_trader.py  (+ mt5_live_engine.py)
        │  HTTPS POST / heartbeat / deals
        ▼
https://35.253.21.246  →  /opt/asim-gcp on GCP
        │
        ├── /live     live desk UI
        └── /         backtest UI (same VM)
```

Auto-update (every 15 min) pulls `BRIDGE_UPDATE_URL` and refreshes Python + MQ5 on the PC. After a desk IP change, fix `.env` first or auto-update will fail.

## Strategy logic on this pack (synced 2026-09-05)

See `docs\TRADING_RULES.md`. Short version:

- **Primary:** previous-body break + X-Trend clear  
- **SUPP:** raw wick break of last trade candle only (`XTREND_GATE_SUPP=0`)  
- **Stops:** trail every candle including entry-bar close (`TRAIL_ENTRY_BAR=1`); entry bar is not stop-tested  
- Fills / stops use **raw MT5 OHLC**, not Heikin Ashi bodies  

## Force refresh after desk publishes a new bundle

```powershell
powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\auto_update_bridge.ps1 -Force
powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\start_bridge_stack.ps1 -Profile ali
```
