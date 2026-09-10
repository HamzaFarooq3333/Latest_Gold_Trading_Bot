# Ali PC — apply GitHub auto-update (candle-safe)

Repo: https://github.com/Onyxion-Corp/Gold_Trading_Bot

## What this update does
1. Detects a new GitHub / bundle version  
2. Waits for the **current M15 candle to close**  
3. Marks the **next** candle as **entry-skip** (logged)  
4. Copies Python bridge files  
5. Restarts watchdog/bridge  
6. **Does not flatten** open trades; keeps `state/` + `.env` + SL memory  

## One-time setup on Ali PC (FamilyHP)

Open **Admin PowerShell**:

```powershell
# 1) Clone the corp repo (once)
mkdir C:\onyxion-src -Force
cd C:\onyxion-src
git clone https://github.com/Onyxion-Corp/Gold_Trading_Bot.git
cd Gold_Trading_Bot
git checkout main

# 2) Point C:\onyxion-ali at Git updates
# Edit C:\onyxion-ali\.env and ADD/SET these lines:
#   BRIDGE_UPDATE_GIT=C:\onyxion-src\Gold_Trading_Bot
#   BRIDGE_UPDATE_GIT_REMOTE=https://github.com/Onyxion-Corp/Gold_Trading_Bot.git
# You can keep BRIDGE_UPDATE_URL as fallback; GIT wins if the folder exists.

# 3) Install / refresh scripts + bridge from the clone
Copy-Item "C:\onyxion-src\Gold_Trading_Bot\Ali PC Deployment\runtime\*" "C:\onyxion-ali\" -Force
Copy-Item "C:\onyxion-src\Gold_Trading_Bot\Ali PC Deployment\scripts\*" "C:\onyxion-ali\scripts\" -Force
New-Item -ItemType Directory -Force -Path "C:\onyxion-ali\mq5" | Out-Null
Copy-Item "C:\onyxion-src\Gold_Trading_Bot\Ali PC Deployment\mq5\*" "C:\onyxion-ali\mq5\" -Force

# 4) Register the scheduled auto-update (every 15 min)
powershell -ExecutionPolicy Bypass -File "C:\onyxion-ali\scripts\register_auto_update_task.ps1" -Profile ali

# 5) Ensure bridge is running
powershell -ExecutionPolicy Bypass -File "C:\onyxion-ali\scripts\start_bridge_stack.ps1" -Profile ali
```

## After each push from the office
Nothing manual on Ali if the task is registered. Optional force now:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\onyxion-ali\scripts\auto_update_bridge.ps1" -Profile ali -Force
Get-Content "C:\onyxion-ali\logs\auto_update.log" -Tail 30
Get-Content "C:\onyxion-ali\CHANGELOG.md" -Tail 40
```

Look for `waited_close=...` and `skip_bar=...` — that candle will have **no new entries** (positions stay open).

## Do not
- Delete `C:\onyxion-ali\state\`
- Overwrite `C:\onyxion-ali\.env`
- Expect MQ5 chart indicators to hot-reload (recompile in MetaEditor only if mq5 files changed)
