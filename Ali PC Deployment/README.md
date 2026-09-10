# Ali PC Deployment

Self-contained pack for Ali’s Windows PC: MetaTrader 5 + Onyxion bridge → Ali GCP live desk.

- **Live:** https://35.253.21.246/live  
- **Backtest (same VM):** https://35.253.21.246/  
- **Login:** ali / 123451  
- **MT5:** 472640728 @ Exness-MT5Trial16 · XAUUSDm · `XTREND_SOURCE=gaga`  
- **Connect guide:** [`CONNECT_LIVE.md`](CONNECT_LIVE.md)  
- **Rules:** [`docs/TRADING_RULES.md`](docs/TRADING_RULES.md)

## Quick start

```powershell
cd "...\Ali PC Deployment"
powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1 -InstallMt5
```

Then follow the printed manual MT5 login / compile / healthcheck / start steps.

## Engine (synced with GCP desk)

| Item | Setting |
|------|---------|
| Primary | Body break + X-Trend clear |
| SUPP | Raw last-trade wick (`XTREND_GATE_SUPP=0`) |
| Trail on entry bar | `TRAIL_ENTRY_BAR=1` |
| Hour filters | **OFF** (`SKIP_WORST_HOURS=0`, `FOCUS_BEST_HOURS=0`) |
| Chart time | **UTC** (MT5 = live desk) |
| MQ5 | Histogram + KJ X-Trend Gaga — `mq5\CHART_SETUP.md` |
| Runtime | `runtime\mt5_live_engine.py` + `runtime\bridge_trader.py` |

After install, force a bundle pull if the desk published a newer zip:

```powershell
powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\auto_update_bridge.ps1 -Force
```

## Claude / Cursor

Give the agent: `prompts\CLAUDE_SETUP_PROMPT.md`

## Status note (ops)

Only **one** GCP desk remains for Ali (`instance-20260831-171822`). It hosts **both** `/live` and the backtest UI `/`. The old Windows MT5 VM (`ali-mt5-windows`) was deleted; execution must run on Ali’s PC (or a new Windows box) using this pack.
