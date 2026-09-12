# Ali push → GCP auto-deploy (Hamza office PC)

## What happens
1. Ali pushes dashboard/bridge code to **Latest** (`HamzaFarooq3333/Latest_Gold_Trading_Bot`).
2. Office watcher `watch_ali_github.ps1` (task `OnyxionAli-GitHubToGcpWatch`) detects Ali’s commit (~30s).
3. Runs **safety_gate** → copies into Hamza’s local workspace → **redeploys GCP desk** (`install.sh` via gcloud or SSH).
4. Live desk updates: https://35.253.21.246/live

Ali’s PC **never** runs gcloud/install. Only this office watcher redeploys GCP.

## Start once on Hamza’s PC
```powershell
cd "d:\Company\Onyxion\Week 7\New folder\New folder\New folder\Google Console Deployment"
.\register_ali_github_watch_task.ps1
```

Keep the PC on (or logged in) so the task can run while you are away.

## When you come back online
Clone / pull Latest — it already has Ali’s code (and your own):

```powershell
git clone https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git
# or, if you already have a clone:
git pull
```

Status of last Ali auto-deploy:
`Google Console Deployment\logs\ali_push_deploy_status.json`

## Related
- Hamza pushes → `watch_hamza_push_deploy.ps1`
- Ali PC bridge-only updates → `github_update_agent.py` on Ali’s machine
