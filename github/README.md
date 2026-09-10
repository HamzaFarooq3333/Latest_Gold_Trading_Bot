# GitHub push — Onyxion Corp

This folder is the **control panel** for uploading the live trading pack to:

**https://github.com/Onyxion-Corp/Gold_Trading_Bot**

It lives next to `Google Console Deployment` and `Ali PC Deployment` in the workspace so you can find it easily. It does **not** replace those packs — the script only stages the directed paths listed below and pushes them.

## Files in this folder

| File | Purpose |
|------|---------|
| `README.md` | This guide |
| `push_to_github.py` | Asks who is pushing (name/email) → commit message → add → commit → push |
| `PUSH_MANIFEST.md` | Exact include/exclude list |
| `run_push.bat` | Double-click helper on Windows |

## How to push

1. Create/open the empty repo under **Onyxion-Corp/Gold_Trading_Bot** and give your account write access.
2. Open a terminal in the workspace root **or** double-click `run_push.bat`.
3. When asked **who is pushing**, enter your **git name** and **git email** (GitHub account email). Press Enter to keep the current git defaults.
4. Enter a commit message.
5. Type **`YES`** to confirm (also accepts `yes`).

```powershell
cd "d:\Company\Onyxion\Week 7\New folder\New folder\New folder"
python .\github\push_to_github.py
```

The script sets author/committer for **that commit only** — it does not change your global git config.

## What gets pushed

- `Google Console Deployment/app/` — GCP live dashboard + engine  
- Selected deploy / env / nginx / systemd files under `Google Console Deployment/`  
- `Ali PC Deployment/` — Python bridge + MQ5 (Histogram, X-Trend Gaga)  
- `TRADING_RULES.md`  
- This `github/` folder (script + docs)

## What does **not** get pushed

Zips, CSV dumps, snapshots, Vintage AWS lambdas, real password `.env` files, SA keys.

## Remote name

Local git remote: **`onyxion-corp`** → `https://github.com/Onyxion-Corp/Gold_Trading_Bot.git`  
Personal `origin` is left unchanged.
