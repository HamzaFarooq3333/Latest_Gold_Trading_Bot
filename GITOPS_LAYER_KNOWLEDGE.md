# Onyxion GitOps + Live Desk — Knowledge Layer

**Repo (source of truth):** https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot  
**Live desk:** https://35.253.21.246/live (login: desk credentials — never share Google/GCP password with Ali)  
**Ali PC root:** `C:\onyxion-ali`  
**Hamza workspace:** `D:\Company\Onyxion\Week 7\New folder\New folder\New folder`

---

## Layer map (everything that was built)

| Layer | Owner | What it does | Key paths |
|---|---|---|---|
| **L0 — Source of truth** | GitHub Latest | Only repo Ali + Hamza share for bridge + desk code | `HamzaFarooq3333/Latest_Gold_Trading_Bot` |
| **L1 — Safety gate** | Both | Compile/presence check before apply or GCP redeploy | `Ali PC Deployment/runtime/safety_gate_check.py`, `*/safety_gate.ps1` |
| **L2 — Ali GitOps agent** | Ali PC | Poll Latest SHA, candle-safe apply, status to desk | `github_update_agent.py`, task `OnyxionAli-GitHubAgent` |
| **L3 — Candle-safe update** | Ali PC | Wait M15 close → skip next bar (X) → gate → copy → restart bridge; never flatten | `auto_update_bridge.ps1`, `state\deploy_skip_bar.json` |
| **L4 — Desk status API** | GCP | Receive Ali status + queue Force check / Restart | `POST /api/broker/ali_pc_status`, `POST /api/broker/ali_pc_command`, `broker.ali_pc` |
| **L5 — Live monitor UI** | GCP | Ali PC panel: timeline YES/NO, processes, gate, buttons, chart X | `static/live_dashboard.html` |
| **L6 — Hamza/Ali → GCP auto-deploy** | Hamza office PC | Any allowed push to Latest → gate → local apply → GCP `install.sh` | `watch_latest_desk_deploy.ps1`, task `OnyxionLatest-DeskDeployWatch` |
| **L7 — Bridge / MT5 runtime** | Ali PC | Trading + heartbeats to desk; one engine shared with the desk | `bridge_trader.py`, `mt5_live_engine.py`, `watchdog_exness.py`, `connection_monitor.py` |
| **L8 — Zero-touch start** | Ali PC | One bat starts stack + agent; four hidden tasks self-heal | `START_BOT.bat`, `scripts\register_tasks.ps1` |
| **L9 — Away mode** | Ali | PC on; if dead run bat only | `ALI_AWAY_RUNBOOK.md` |
| **L10 — Config precedence** | Both | `.env` on Ali PC governs; desk controls only once saved on the dashboard | `bridge_trader.apply_live_controls`, `broker_live.CONTROL_KEYS` |

---

## Who does what

| Action | Ali PC | Hamza office / GCP |
|---|---|---|
| Edit / push dashboard code | Can push to Latest | Same |
| Redeploy GCP desk | **Never** | Auto via `OnyxionLatest-DeskDeployWatch` or manual SSH/`install.sh` |
| Update bridge/runtime | Auto via `github_update_agent` | Optional pack sync |
| Flatten trades on update | **Never** | **Never** |
| Share Google password | **No** | Keep private |

---

## Ali PC status (what the desk shows)

`sync_timeline` / Update timeline card:

- Last GitHub push time / author / message  
- Ali detected change? **YES/NO**  
- Ali started clone+apply? **YES/NO**  
- Last successful pull + safety test + bridge restart  
- Bridge in sync with GitHub? **YES/NO**  

Remote buttons:

- **Force GitHub check** — poll Latest now (does not abort candle wait)  
- **Restart stack** — safe restart; deferred if candle-wait in flight  

AGENT OFFLINE = no status POST for >90s.

---

## Env keys (Ali `.env`)

```
ASIM_LAB_URL=https://35.253.21.246
ASIM_LAB_INSECURE=1
BRIDGE_UPDATE_GIT=C:\onyxion-src\Latest_Gold_Trading_Bot
BRIDGE_UPDATE_GIT_REMOTE=https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git
```

Clone Latest once to `BRIDGE_UPDATE_GIT` before the agent can apply.

---

## Logs

| Where | File |
|---|---|
| Ali agent | `C:\onyxion-ali\logs\github_update_agent.log` |
| Ali last apply | `C:\onyxion-ali\state\last_successful_apply.json` |
| Ali skip bar | `C:\onyxion-ali\state\deploy_skip_bar.json` |
| Desk auto-deploy | `Google Console Deployment\logs\latest_desk_deploy.log` |
| Deploy status JSON | `...\logs\latest_desk_deploy_status.json` |

---

## Rollback

1. `git revert` (or re-release) the good SHA on `main` of Latest
2. Office watcher redeploys the desk
3. Ali agent pulls candle-safe (open positions untouched)

---

## Commit / push discipline

Before any GitHub push: summarise the change, agree the commit message, then commit/push.
Style: `desk:`, `bridge:`, `gitops:`, `test:`, `docs:`.

---

## Tests (feature coverage)

Ali PC runtime (`python -m unittest discover -p "test_*.py"` in `Ali PC Deployment/runtime`):

- `test_engine_rules.py` - every-candle stacking, ATR stop, close-trailing, amber flatten, classic mode, live-read controls, ticket reconciliation
- `test_safety_gate.py`, `test_deploy_skip_bar.py`, `test_github_agent_status.py`, `test_candle_wait_timing.py`, `test_restart_request_safe.py`

Desk app (`Google Console Deployment/app`): `test_ali_pc_api.py`, `test_bridge_entry_dedupe.py`, `test_skip_bar_marker_ui.py`

---

## Out of scope (never)

- Ali runs `gcloud` / `install.sh`  
- Sharing GCP/Google passwords with Ali  
- Flattening positions for code updates  
