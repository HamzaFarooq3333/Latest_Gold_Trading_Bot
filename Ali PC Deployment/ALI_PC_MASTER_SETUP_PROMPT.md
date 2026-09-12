# Ali PC — Master setup prompt (copy/paste for Claude / Cursor / technician)

Use this prompt on Ali’s machine or with an agent that has access to Ali’s PC.

---

## PROMPT START

You are setting up **Onyxion Ali PC** so it trades via Exness MT5 and syncs with the live GCP desk, with candle-safe GitHub updates from Latest only.

### Goals
1. Bridge + MT5 + connection monitor + GitHub update agent all running under `C:\onyxion-ali`.
2. Heartbeats reach `https://35.253.21.246/live`.
3. Agent polls `https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot` and applies updates candle-safe (never flatten, never redeploy GCP).
4. Desk Ali PC panel shows status (detect YES/NO, apply YES/NO, last success).

### Non-negotiables
- Do **not** ask for or use Google/GCP passwords.
- Do **not** run `gcloud`, `install.sh`, or any GCP desk redeploy from Ali PC.
- Do **not** flatten or close trades for updates.
- Source of truth repo: **HamzaFarooq3333/Latest_Gold_Trading_Bot** only.

### Step A — Files on disk
1. Ensure pack exists at `C:\onyxion-ali` with at least:
   - `bridge_trader.py`, `mt5_live_engine.py`, `watchdog_exness.py`
   - `connection_monitor.py`, `github_update_agent.py`, `safety_gate_check.py`
   - `scripts\auto_update_bridge.ps1`, `scripts\safety_gate.ps1`, `scripts\start_bridge_stack.ps1`
   - `START_BOT.bat`, `VERSION.json`, `.env`
2. If missing runtime files: clone Latest and copy from `Ali PC Deployment\runtime` + `scripts` into `C:\onyxion-ali` (preserve `.env` and `state\`).

### Step B — Git clone for updates
```
git clone https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git C:\onyxion-src\Latest_Gold_Trading_Bot
```
Ali needs GitHub access as collaborator `alimacbookpro-web` (or equivalent).

### Step C — `.env` (edit real secrets locally; never commit)
Must include:
```
ASIM_LAB_URL=https://35.253.21.246
ASIM_LAB_INSECURE=1
BRIDGE_UPDATE_GIT=C:\onyxion-src\Latest_Gold_Trading_Bot
BRIDGE_UPDATE_GIT_REMOTE=https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git
ASIM_MT5_LOGIN=...
ASIM_MT5_PASSWORD=...
ASIM_MT5_SERVER=...
ASIM_MT5_SYMBOL=XAUUSDm
XTREND_SOURCE=gaga
```
Keep existing lot/trail settings unless Hamza says otherwise.

### Step D — Start stack
1. Enable **Algo Trading** in MT5.
2. Run `C:\onyxion-ali\START_BOT.bat`.
3. Confirm it starts: MT5, watchdog/bridge, connection monitor, GitHub agent (`OnyxionAli-GitHubAgent`).
4. If task register fails, start agent manually:
```
C:\onyxion-ali\venv\Scripts\pythonw.exe C:\onyxion-ali\github_update_agent.py
```

### Step E — Verify (checklist)
On Ali PC:
- [ ] `logs\bridge_asim.log` or bridge log shows heartbeats / no restart loop
- [ ] `logs\github_update_agent.log` exists and polls without crash
- [ ] `logs\connection_check.log` shows CONNECTED periodically
- [ ] Git dir `C:\onyxion-src\Latest_Gold_Trading_Bot` has `.git`

On desk https://35.253.21.246/live (ali desk login):
- [ ] BRIDGE LIVE (heartbeat age small)
- [ ] Open **Ali PC** panel
- [ ] Not stuck AGENT OFFLINE (>90s)
- [ ] Update timeline shows last poll / SHAs
- [ ] Force GitHub check queues without error
- [ ] Chart can show **X skip** after an update skip bar

### Step F — Away mode (Ali weeks away)
- Leave PC **on**, MT5 logged in, Algo on.
- If everything dies: run **only** `C:\onyxion-ali\START_BOT.bat`.
- Do not redeploy GCP. Hamza’s office watcher handles desk; Ali agent handles bridge.

### Step G — What success looks like after Hamza/Ali push to Latest
1. Within ~1–2 min: Ali panel **detected=YES** (if behind).
2. Up to ~15 min: may **waiting_close** then apply (candle-safe).
3. Then **update_ok**, bridge restart, MT5 still open, positions kept.
4. Desk UI updates only when Hamza-side GCP redeploy runs — Ali does not do that.

### If something fails
| Symptom | Check |
|---|---|
| AGENT OFFLINE | Agent process/task; `github_update_agent.log`; `ASIM_LAB_URL` |
| BEHIND forever | safety_gate errors; git credentials; `last_update_result.json` |
| No heartbeats | MT5 Algo; watchdog; firewall; desk URL |
| Desk old UI | Normal on Ali — GCP redeploy is Hamza office only |

Read knowledge: repo file `GITOPS_LAYER_KNOWLEDGE.md` and `Ali PC Deployment/ALI_AWAY_RUNBOOK.md`.

## PROMPT END
