# Push pack for https://github.com/Onyxion-Corp/Gold_Trading_Bot

Target remote (local name): `onyxion-corp` → `https://github.com/Onyxion-Corp/Gold_Trading_Bot.git`

Use: `python github/push_to_github.py` or `github/run_push.bat`

## Include (live system only)

| Path | Role |
|------|------|
| `Google Console Deployment/app/` | GCP live + backtest dashboards, engine, broker API |
| `Google Console Deployment/` selected deploy/env/nginx/systemd files | Redeploy the VM desk |
| `Ali PC Deployment/` | Windows MT5 bridge, watchdog, MQ5 |
| `TRADING_RULES.md` | Strategy rules |
| `github/` | This push helper |

## Exclude

- Zips, snapshots, CSV dumps, `XAUUSDm/`
- Real `.env` passwords, SA keys
- Vintage AWS lambda stacks
- One-off `_persist_*` scripts
