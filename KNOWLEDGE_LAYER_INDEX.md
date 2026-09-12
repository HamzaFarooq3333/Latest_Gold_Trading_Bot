# Knowledge layer index

| Doc | Purpose |
|---|---|
| [GITOPS_LAYER_KNOWLEDGE.md](GITOPS_LAYER_KNOWLEDGE.md) | Full layer map L0–L12, APIs, env, logs, rollback |
| [Ali PC Deployment/ALI_PC_MASTER_SETUP_PROMPT.md](Ali%20PC%20Deployment/ALI_PC_MASTER_SETUP_PROMPT.md) | Copy-paste master prompt to set up / verify Ali PC |
| [Ali PC Deployment/ALI_AWAY_RUNBOOK.md](Ali%20PC%20Deployment/ALI_AWAY_RUNBOOK.md) | Away one-liner |
| [Google Console Deployment/ALI_PUSH_AUTO_DEPLOY.md](Google%20Console%20Deployment/ALI_PUSH_AUTO_DEPLOY.md) | Ali push → GCP (legacy Ali-only watcher) |
| [ROLLBACK/README.txt](ROLLBACK/README.txt) | Pin good SHAs / rollback |
| [ROLLBACK/COMMIT_STYLE.txt](ROLLBACK/COMMIT_STYLE.txt) | Commit message style |

Unified desk auto-deploy (Hamza **or** Ali push → GCP):  
`Google Console Deployment/watch_latest_desk_deploy.ps1`  
Task: `OnyxionLatest-DeskDeployWatch`
