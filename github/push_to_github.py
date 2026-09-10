"""
Push only the Onyxion live desk pack to:
  https://github.com/Onyxion-Corp/Gold_Trading_Bot

Run:
  python github/push_to_github.py
  — or double-click github/run_push.bat

Does NOT push Vintage AWS stacks, zips, dumps, or secrets.
Asks who is pushing (git author name + email) before commit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Repo root = parent of this github/ folder
ROOT = Path(__file__).resolve().parent.parent
REMOTE = "onyxion-corp"
REMOTE_URL = "https://github.com/Onyxion-Corp/Gold_Trading_Bot.git"
BRANCH = "main"

# Only these paths are staged / pushed
INCLUDE_PATHS = [
    "Google Console Deployment/app",
    "Google Console Deployment/asim-gcp.service",
    "Google Console Deployment/nginx-asim-gcp.conf",
    "Google Console Deployment/install.sh",
    "Google Console Deployment/deploy.ps1",
    "Google Console Deployment/deploy_best_xauusdm_engine.ps1",
    "Google Console Deployment/deploy_ali_gaga_xtrend.ps1",
    "Google Console Deployment/fix_access_and_deploy.ps1",
    "Google Console Deployment/fresh_wipe_before_start.ps1",
    "Google Console Deployment/open_public_firewall_permanent.ps1",
    "Google Console Deployment/OPEN_SITE_PUBLIC_NOW.ps1",
    "Google Console Deployment/start_ali_interactive.ps1",
    "Google Console Deployment/env.instance-20260831-171822",
    "Google Console Deployment/env.bridge.hamzatestserver01",
    "Google Console Deployment/env.hamzatestserver01",
    "Google Console Deployment/.env.example",
    "Google Console Deployment/best_controls.json",
    "Google Console Deployment/README.md",
    "Ali PC Deployment",
    "TRADING_RULES.md",
    "github",
]


def run(
    cmd: list[str],
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd))
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        check=check,
        text=True,
        capture_output=False,
        env=merged,
    )


def run_out(cmd: list[str]) -> str:
    p = subprocess.run(
        cmd,
        cwd=str(ROOT),
        check=False,
        text=True,
        capture_output=True,
    )
    return (p.stdout or "").strip()


def ensure_remote() -> None:
    remotes = run_out(["git", "remote"])
    names = set(remotes.splitlines()) if remotes else set()
    if REMOTE not in names:
        print(f"Adding remote {REMOTE} -> {REMOTE_URL}")
        run(["git", "remote", "add", REMOTE, REMOTE_URL])
    else:
        current = run_out(["git", "remote", "get-url", REMOTE])
        if current != REMOTE_URL:
            print(f"Updating {REMOTE} URL:\n  was: {current}\n  now: {REMOTE_URL}")
            run(["git", "remote", "set-url", REMOTE, REMOTE_URL])
        else:
            print(f"Remote OK: {REMOTE} -> {REMOTE_URL}")


def existing_includes() -> list[str]:
    found = []
    missing = []
    for rel in INCLUDE_PATHS:
        p = ROOT / rel
        if p.exists():
            found.append(rel)
        else:
            missing.append(rel)
    if missing:
        print("WARNING — missing paths (skipped):")
        for m in missing:
            print(" ", m)
    if not found:
        raise SystemExit("No include paths found — abort.")
    return found


def ask_who_is_pushing() -> tuple[str, str]:
    """Ask for git author; does not change git config — only this commit."""
    default_name = run_out(["git", "config", "user.name"]) or ""
    default_email = run_out(["git", "config", "user.email"]) or ""

    print("Who is pushing? (shown as GitHub commit author)")
    if default_name or default_email:
        print(f"  Current git defaults: {default_name} <{default_email}>")
    print("  Press Enter to keep a default, or type a new value.")
    print()

    name = input(f"Your git name [{default_name}]: ").strip() or default_name
    email = input(f"Your git email [{default_email}]: ").strip() or default_email

    if not name or not email:
        raise SystemExit("Name and email are required — abort.")
    if "@" not in email:
        raise SystemExit("Email looks invalid (missing @) — abort.")

    print(f"Commit will be authored as: {name} <{email}>")
    return name, email


def main() -> int:
    print("Repo root:", ROOT)
    print("Target:   ", REMOTE_URL)
    print()
    print("Folders / files that will be added:")
    paths = existing_includes()
    for rel in paths:
        print(" ", rel)
    print()

    author_name, author_email = ask_who_is_pushing()
    print()

    msg = input("Commit message: ").strip()
    if not msg:
        print("Empty commit message — abort.")
        return 1

    confirm = input(f"Type YES to add + commit + push to {REMOTE}/{BRANCH}: ").strip()
    if confirm.upper() != "YES":
        print("Cancelled.")
        return 1

    ensure_remote()

    run(["git", "add", "--"] + paths)

    staged = run_out(["git", "diff", "--cached", "--name-only"])
    author_env = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }

    if not staged:
        print("Nothing staged — working tree already matches for these paths.")
        push = input(f"Still push current {BRANCH} to {REMOTE}? [y/N]: ").strip().lower()
        if push != "y":
            return 0
    else:
        print("Staged files:")
        print(staged)
        # Author/committer for THIS commit only (does not change git config).
        run(["git", "commit", "-m", msg], env=author_env)
        # Show what was recorded
        print(run_out(["git", "log", "-1", "--format=Author: %an <%ae>%nCommitter: %cn <%ce>"]))

    print(f"Pushing to {REMOTE} ({BRANCH}) ...")
    run(["git", "push", "-u", REMOTE, f"HEAD:{BRANCH}"])
    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as e:
        print(f"git failed with exit {e.returncode}", file=sys.stderr)
        raise SystemExit(e.returncode)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)
