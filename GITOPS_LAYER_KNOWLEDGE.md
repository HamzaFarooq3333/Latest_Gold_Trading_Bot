# Onyxion GitOps + Live Desk — Knowledge Layer

**Repo (source of truth):** https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot  
**Live desk:** https://35.253.21.246/live (login: desk credentials — never share Google/GCP password with Ali)  
**Ali PC root:** `C:\onyxion-ali`  
**Hamza workspace:** `D:\Company\Onyxion\Week 7\New folder\New folder\New folder`  
**Rollback pins:** `ROLLBACK\GOOD_SHA.txt` (pin only when you say so)

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
| **L7 — Ali-only watcher (legacy)** | Hamza office | Ali-author pushes only | `watch_ali_github.ps1` |
| **L8 — Hamza-only watcher** | Hamza office | Hamza-author pushes only | `watch_hamza_push_deploy.ps1` |
| **L9 — Bridge / MT5 runtime** | Ali PC | Trading + heartbeats to desk | `bridge_trader.py`, `mt5_live_engine.py`, `watchdog_exness.py`, `connection_monitor.py` |
| **L10 — Zero-touch start** | Ali PC | One bat starts stack + agent | `START_BOT.bat` |
| **L11 — Away mode** | Ali | PC on; if dead run bat only | `ALI_AWAY_RUNBOOK.md` |
| **L12 — Rollback discipline** | Hamza | Pin good SHAs; ask before push/commits | `ROLLBACK\`, rule `.cursor/rules/github-push-approval.mdc` |

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

1. Read `ROLLBACK\GOOD_SHA.txt`  
2. Restore that SHA on Latest (revert or re-release)  
3. Office watcher redeploys desk  
4. Ali agent pulls candle-safe  

Pin only when Hamza says: `pin SHA <sha> as good — <reason>`.

---

## Commit / push discipline (Hamza)

Before any GitHub push, assistant must:

1. Summarize changes  
2. Propose or ask for commit message(s)  
3. Wait for explicit OK  
4. Then commit/push  

Style: `desk:`, `bridge:`, `gitops:`, `test:`, `docs:` — see `ROLLBACK\COMMIT_STYLE.txt`.

---

## Tests (feature coverage)

- `test_safety_gate.py`  
- `test_deploy_skip_bar.py`  
- `test_github_agent_status.py`  
- `test_candle_wait_timing.py`  
- `test_restart_request_safe.py`  
- `test_ali_pc_api.py`  
- `test_skip_bar_marker_ui.py`  
- Live smoke: `_feature_smoke_test.py`  

---

## Out of scope (never)

- Ali runs `gcloud` / `install.sh`  
- Sharing GCP/Google passwords with Ali  
- Flattening positions for code updates  
