# Ali PC Deployment

Self-contained pack for Ali's Windows PC: portable MetaTrader 5 + the Onyxion bridge → Ali GCP live desk.

- **Live:** https://35.253.21.246/live · **Backtest:** https://35.253.21.246/ (login: desk credentials)
- **MT5:** demo `472640728` @ `Exness-MT5Trial16` · `XAUUSDm` M15 · `XTREND_SOURCE=gaga`
- **Rules:** [`../TRADING_RULES.md`](../TRADING_RULES.md)
- **Install root on the PC:** `C:\onyxion-ali` (venv, flat runtime, `scripts\`, `mq5\`, `state\`, `logs\`)

## Quick start

```powershell
cd "...\Ali PC Deployment"
powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1
```

Then: fill `C:\onyxion-ali\.env` (password), install the portable terminal to `C:\onyxion-ali\exness-mt5`, run `START_BOT.bat`.

## What runs

| Process | File | Job |
|---------|------|-----|
| bridge | `bridge_trader.py` | closed-bar rules engine → MT5 orders → desk heartbeat every 2 s |
| watchdog | `watchdog_exness.py` | restarts MT5 / bridge if either dies |
| connection monitor | `connection_monitor.py` | 5-min health checks, MT5 relaunch, stale-code restart |
| GitHub agent | `github_update_agent.py` | candle-safe pull + apply from `Latest_Gold_Trading_Bot`, status to the desk |

All four run as hidden scheduled tasks (`scripts\register_tasks.ps1`). `START_BOT.bat` / `CHECK_BOT.bat` / `STOP_BOT.bat` are the manual controls.

## Live configuration (`.env`)

| Item | Setting |
|------|---------|
| Entry | primary gate (body break + X-Trend clear) on **every** green/red candle, tickets stack (`ENTRY_EVERY_CANDLE=1`) |
| Threshold | `HIST_THRESH=10` |
| Stop | `1.25 × ATR(14)` per ticket, trails each close (`TSL_ATR_MULT=1.25`) |
| Lot | `VOLUME=0.02`, `MAXPOS=20`, `MAX_SUPP=0` |
| Filters | hour / weekend filters OFF |

`.env` wins; desk *Live controls* apply only once saved on the dashboard.

## Tests

```powershell
cd runtime
C:\onyxion-ali\venv\Scripts\python.exe -m unittest discover -p "test_*.py"
```

## Reports

`backtest_report.html`, `strategy_report.html`, `backtest_6month.html`, `backtest_6month_bodyonly.html`, `live_trade_analysis.html` — generated from `XAUUSDm_M15_202601012300_202608271930.csv` and the live desk.
