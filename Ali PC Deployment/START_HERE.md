# Ali PC Deployment — START HERE

**What this pack is:** everything Ali needs on a **Windows PC** to run MetaTrader 5, the Onyxion bridge, and push live ticks/trades to the **Ali GCP desk**.

**GCP desk (one VM does both live + backtest):**
| Role | URL | Login |
|------|-----|-------|
| Live trading dashboard | https://35.253.21.246/live | `ali` / `123451` |
| Backtest UI | https://35.253.21.246/ | `ali` / `123451` |
| Bridge auto-update zip | https://35.253.21.246/bridge/onyxion-bridge-bundle.zip | (no auth; firewall IP only) |

VM name: `instance-20260831-171822` (us-central1-a).  
**If the VM is stopped/started, the external IP can change** — update `ASIM_LAB_URL` and `BRIDGE_UPDATE_URL` in `C:\onyxion-ali\.env`.

Full connect checklist: **`CONNECT_LIVE.md`**.

---

## 5-minute outline

1. Copy this whole **`Ali PC Deployment`** folder to Ali’s Windows PC.
2. Install **Python 3.11+ 64-bit** (check “Add to PATH”).
3. Open **Admin PowerShell**:

```powershell
cd "PATH\TO\Ali PC Deployment"
powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1 -InstallMt5
```

4. Open MT5 → log in demo **472640728** / server **Exness-MT5Trial16** → enable Algo Trading → XAUUSDm **M15**.
5. Compile MQ5 and attach **OnyxionHistogram** + **OnyxionXTrendProxy** (KJ Gaga) — see `mq5\CHART_SETUP.md`.
6. Healthcheck, then start bridge (commands printed by INSTALL.ps1). Keep Python/watchdog running.
7. Confirm https://35.253.21.246/live shows MT5 online and **UTC** bar times matching MT5.

**Using Claude/Cursor on Ali’s PC?** Paste `prompts\CLAUDE_SETUP_PROMPT.md` into the chat (full step-by-step).

---

## Account (Exness demo)

| Field | Value |
|-------|-------|
| Login | `472640728` |
| Server | `Exness-MT5Trial16` |
| Symbol | `XAUUSDm` |
| Password | in `env\.env.ali.example` → copied to `C:\onyxion-ali\.env` |
| X-Trend | **gaga** (KJ GagaTrend) — required for Ali |

---

## Engine rules on this pack (synced with live desk)

| Rule | Value |
|------|--------|
| Primary entry | Previous-body break + X-Trend clear |
| SUPP entry | **Raw** wick of last trade candle only (`XTREND_GATE_SUPP=0`) |
| Entry-bar trail | **ON** (`TRAIL_ENTRY_BAR=1`) — stop trails to entry-bar close; entry bar is not stop-tested |
| Fills / stops | Raw MT5 OHLC (not HA body size) |
| Hour filters | **OFF** (`SKIP_WORST_HOURS=0`, `FOCUS_BEST_HOURS=0`) |
| Chart clock | **UTC** on MT5 and live desk (same M15 bar times) |
| Details | `docs\TRADING_RULES.md` |

---

## Folder map

```
Ali PC Deployment/
  INSTALL.ps1                 ← one-shot installer
  START_HERE.md               ← this file
  CONNECT_LIVE.md             ← MT5 ↔ GCP live link checklist
  README.md
  env/.env.ali.example        ← secrets template + live URLs
  runtime/                    ← bridge Python code (synced with GCP desk)
  scripts/                    ← install / start / auto-update / sync_runtime_from_gcp_app.ps1
  mq5/                        ← Histogram + KJ X-Trend (Gaga) + EAs; see CHART_SETUP.md
  dist/onyxion-bridge-bundle.zip  ← same zip published to GCP /bridge/
  docs/TRADING_RULES.md
  prompts/CLAUDE_SETUP_PROMPT.md  ← check install → start one-by-one → verify GCP → keep Python up
```

After install, live code lives in **`C:\onyxion-ali\`**. To refresh engine files from the repo before shipping the pack:

```powershell
powershell -ExecutionPolicy Bypass -File ".\Ali PC Deployment\scripts\sync_runtime_from_gcp_app.ps1"
```

---

## Firewall (important)

Ali’s **public IP** (`https://ifconfig.me`) must be allowed on GCP firewall rule `allow-asim-live-443` as `/32`.  
If dashboard or bundle download fails, send the public IP to Onyxion to whitelist.

---

## Safety

- Demo account only.
- Do not commit `.env` or share passwords in public chat.
- Do not use `--force-action` on the bridge unless Onyxion asks.
