"""Candle-safe GitHub update agent for Ali PC.

Polls Latest_Gold_Trading_Bot origin/main, waits for M15 close, writes
deploy_skip_bar, runs safety_gate, then applies via auto_update_bridge.ps1.
Posts full status to the GCP live desk. Never redeploys the desk.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("MT5_ROOT", r"C:\onyxion-ali"))
ENV_FILE = ROOT / ".env"
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"
STATUS_FILE = STATE_DIR / "github_agent_status.json"
RESULT_FILE = STATE_DIR / "last_update_result.json"
SKIP_FILE = STATE_DIR / "deploy_skip_bar.json"
LOCK_FILE = STATE_DIR / "github_update_agent.lock"
AGENT_LOG = LOG_DIR / "github_update_agent.log"

POLL_SECONDS = int(os.environ.get("GITHUB_AGENT_POLL_SEC", "90"))
STATUS_SECONDS = int(os.environ.get("GITHUB_AGENT_STATUS_SEC", "20"))
DEFAULT_REMOTE = "https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git"

LOCK_HANDLE = None


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{_stamp()} UTC {msg}"
    with AGENT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    try:
        print(line, flush=True)
    except Exception:
        pass


def acquire_lock() -> bool:
    global LOCK_HANDLE
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        LOCK_HANDLE = open(LOCK_FILE, "a+", encoding="utf-8")
        if sys.platform == "win32":
            import msvcrt

            LOCK_HANDLE.seek(0)
            msvcrt.locking(LOCK_HANDLE.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        LOCK_HANDLE.seek(0)
        LOCK_HANDLE.truncate()
        LOCK_HANDLE.write(str(os.getpid()))
        LOCK_HANDLE.flush()
        return True
    except OSError:
        return False


def m15_open_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    minute = (now.minute // 15) * 15
    return now.replace(minute=minute, second=0, microsecond=0)


def candle_wait_plan(now: datetime | None = None) -> dict:
    """Pure timing helper: current M15 open, close, and next-bar skip time."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    current_open = m15_open_utc(now)
    close_at = current_open + timedelta(minutes=15)
    skip_bar = close_at
    wait_sec = max(3, int((close_at - now).total_seconds()) + 3)
    wait_sec = min(wait_sec, 960)
    return {
        "waited_close_bar": current_open.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skip_bar_time": skip_bar.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wait_sec": wait_sec,
        "close_at_utc": close_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_deploy_skip_marker(
    *,
    skip_bar_time: str,
    waited_close_bar: str,
    version: str = "unknown",
) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "skip_bar_time": skip_bar_time,
        "waited_close_bar": waited_close_bar,
        "version": version,
        "reason": "github_agent_skip_next_bar",
        "no_flatten": True,
        "preserve_positions": True,
        "preserve_sl": True,
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    SKIP_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return SKIP_FILE


def read_json(path: Path, default=None):
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def write_status(blob: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    blob = dict(blob)
    blob["written_at"] = datetime.now(timezone.utc).isoformat()
    STATUS_FILE.write_text(json.dumps(blob, indent=2), encoding="utf-8")


def git_sha(cwd: Path, ref: str = "HEAD") -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", ref],
            cwd=str(cwd),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        return out.strip() or None
    except Exception:
        return None


def remote_main_sha(git_dir: Path, remote_url: str | None) -> str | None:
    try:
        if remote_url:
            subprocess.run(
                ["git", "remote", "set-url", "origin", remote_url],
                cwd=str(git_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        out = subprocess.check_output(
            ["git", "ls-remote", "origin", "refs/heads/main"],
            cwd=str(git_dir),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=60,
        )
        line = (out or "").strip().splitlines()[0] if out.strip() else ""
        return line.split()[0] if line else None
    except Exception as exc:
        log(f"remote sha error: {exc}")
        return None


def process_info(patterns: list[str]) -> dict:
    """Best-effort Windows process lookup by command line substring."""
    info = {"running": False, "pid": None, "uptime_sec": None}
    if sys.platform != "win32":
        return info
    try:
        # wmic is deprecated but widely present; fall back silently.
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "$p = Get-CimInstance Win32_Process | Where-Object { "
                + " -or ".join([f"$_.CommandLine -like '*{pat}*'" for pat in patterns])
                + " } | Select-Object -First 1 ProcessId,CreationDate; "
                "if ($p) { $age = [int]((Get-Date) - $p.CreationDate).TotalSeconds; "
                "Write-Output ($p.ProcessId.ToString() + '|' + $age) }"
            ),
        ]
        out = subprocess.check_output(cmd, text=True, timeout=20, stderr=subprocess.DEVNULL)
        line = (out or "").strip()
        if "|" in line:
            pid_s, age_s = line.split("|", 1)
            info["running"] = True
            info["pid"] = int(pid_s)
            info["uptime_sec"] = int(age_s)
    except Exception:
        pass
    return info


def run_safety_gate(check_root: Path) -> dict:
    checker = ROOT / "safety_gate_check.py"
    if not checker.is_file():
        checker = ROOT / "scripts" / "safety_gate_check.py"
    py = sys.executable
    try:
        proc = subprocess.run(
            [py, str(checker), "--root", str(check_root), "--json", "--no-dashboard"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        raw = (proc.stdout or "").strip()
        data = json.loads(raw) if raw.startswith("{") else {"ok": proc.returncode == 0, "errors": [raw or proc.stderr]}
        data["returncode"] = proc.returncode
        return data
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)], "returncode": 1}


def invoke_auto_update(*, force: bool = True, skip_wait: bool = False) -> int:
    script = ROOT / "scripts" / "auto_update_bridge.ps1"
    if not script.is_file():
        log(f"missing {script}")
        return 1
    args = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Profile",
        "ali",
        "-Root",
        str(ROOT),
    ]
    if force:
        args.append("-Force")
    if skip_wait:
        args.append("-SkipCandleWait")
    try:
        proc = subprocess.run(args, cwd=str(ROOT), timeout=1200)
        return int(proc.returncode)
    except Exception as exc:
        log(f"auto_update error: {exc}")
        return 1


def restart_stack_safe(*, in_flight_wait: bool) -> dict:
    """Restart dead/connection stack without aborting an in-flight candle wait."""
    if in_flight_wait:
        return {
            "ok": True,
            "deferred": True,
            "note": "restart queued after candle wait (did not abort skip wait)",
        }
    script = ROOT / "scripts" / "start_bridge_stack.ps1"
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Profile",
                "ali",
                "-Root",
                str(ROOT),
            ],
            timeout=180,
        )
        return {"ok": True, "deferred": False, "note": "start_bridge_stack invoked"}
    except Exception as exc:
        return {"ok": False, "deferred": False, "note": str(exc)}


def post_status(env: dict, payload: dict) -> dict:
    base = (env.get("ASIM_LAB_URL") or os.environ.get("ASIM_LAB_URL") or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "ASIM_LAB_URL missing"}
    url = base + "/api/broker/ali_pc_status"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "onyxion-github-agent"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    insecure = (env.get("ASIM_LAB_INSECURE") or "1").strip().lower() not in ("0", "false", "no")
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def disk_free_gb(path: Path) -> float | None:
    try:
        usage = shutil.disk_usage(str(path))
        return round(usage.free / (1024**3), 2)
    except Exception:
        return None


def pc_uptime_sec() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')",
            ],
            text=True,
            timeout=15,
        )
        boot = datetime.fromisoformat(out.strip().replace("Z", "+00:00"))
        if boot.tzinfo is None:
            boot = boot.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - boot).total_seconds())
    except Exception:
        return None


def fetch_remote_commit_meta(remote_url: str | None) -> dict:
    """Best-effort Latest tip metadata from GitHub API (no token required for public repos)."""
    repo = "HamzaFarooq3333/Latest_Gold_Trading_Bot"
    if remote_url and "github.com" in remote_url:
        # https://github.com/owner/repo.git → owner/repo
        try:
            part = remote_url.rstrip("/").split("github.com/")[-1]
            part = part.replace(".git", "").strip("/")
            if part.count("/") == 1:
                repo = part
        except Exception:
            pass
    url = f"https://api.github.com/repos/{repo}/commits/main"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "onyxion-github-agent",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        commit = data.get("commit") or {}
        author = commit.get("author") or {}
        return {
            "sha": data.get("sha"),
            "pushed_at_utc": author.get("date"),
            "author_name": author.get("name") or (data.get("author") or {}).get("login"),
            "author_login": (data.get("author") or {}).get("login"),
            "message": ((commit.get("message") or "").splitlines() or [""])[0][:160],
            "html_url": data.get("html_url"),
            "repo": repo,
        }
    except Exception as exc:
        return {"error": str(exc), "repo": repo}


def yes_no(flag: bool | None) -> str:
    if flag is True:
        return "YES"
    if flag is False:
        return "NO"
    return "UNKNOWN"


def build_sync_timeline(state: dict) -> dict:
    """Plain-language YES/NO timeline for the live Ali PC panel."""
    local = state.get("local_sha")
    remote = state.get("remote_sha") or state.get("remote_push_sha")
    behind = bool(local and remote and local != remote)
    upd = str(state.get("update_state") or "idle")
    last_ok = state.get("last_successful_apply_utc")
    last_result = state.get("last_result")
    return {
        "last_github_push_utc": state.get("remote_push_utc"),
        "last_github_push_by": state.get("remote_push_author"),
        "last_github_push_sha": (remote or "")[:12] or None,
        "last_github_push_message": state.get("remote_push_message"),
        "ali_detected_github_change": yes_no(behind),
        "ali_detected_github_change_bool": behind,
        "ali_started_clone_and_apply": yes_no(
            upd in ("waiting_close", "applying", "restarting")
        ),
        "ali_started_clone_and_apply_bool": upd
        in ("waiting_close", "applying", "restarting"),
        "apply_phase": upd,
        "apply_started_at_utc": state.get("apply_started_at"),
        "last_successful_pull_test_restart_utc": last_ok,
        "last_successful_apply_sha": state.get("last_successful_apply_sha"),
        "last_successful_apply_result": last_result if last_result == "update_ok" else (
            "update_ok" if last_ok else last_result
        ),
        "bridge_in_sync_with_github": yes_no(bool(local and remote and local == remote)),
        "notes": (
            "Desk/GCP redeploy is Hamza-only. Ali PC only pulls bridge/runtime "
            "(candle-safe; never flattens; never runs install.sh)."
        ),
    }


def build_payload(state: dict, env: dict) -> dict:
    local = state.get("local_sha")
    remote = state.get("remote_sha")
    behind = bool(local and remote and local != remote)
    timeline = build_sync_timeline(state)
    return {
        "host": os.environ.get("COMPUTERNAME") or "ali-pc",
        "profile": "ali",
        "github_checker": {
            "running": True,
            "last_poll_utc": state.get("last_poll_utc"),
            "local_sha": local,
            "remote_sha": remote,
            "behind": behind,
            "git_dir": state.get("git_dir"),
            "remote": state.get("remote_url"),
            "last_github_push_utc": state.get("remote_push_utc"),
            "last_github_push_by": state.get("remote_push_author"),
            "last_github_push_message": state.get("remote_push_message"),
            "ali_detected_change": timeline["ali_detected_github_change"],
        },
        "update": {
            "state": state.get("update_state", "idle"),
            "last_result": state.get("last_result"),
            "skip_bar_time": state.get("skip_bar_time"),
            "waited_close_bar": state.get("waited_close_bar"),
            "last_restart_utc": state.get("last_restart_utc"),
            "blocked_error": state.get("blocked_error"),
            "apply_started_at_utc": state.get("apply_started_at"),
            "last_successful_pull_test_restart_utc": state.get("last_successful_apply_utc"),
            "ali_started_clone_and_apply": timeline["ali_started_clone_and_apply"],
        },
        "sync_timeline": timeline,
        "processes": state.get("processes") or {},
        "mt5": state.get("mt5") or {},
        "desk_link": state.get("desk_link") or {},
        "code": state.get("code") or {},
        "risk": state.get("risk") or {},
        "safety_gate": state.get("safety_gate") or {},
        "pc": {
            "uptime_sec": pc_uptime_sec(),
            "disk_free_gb": disk_free_gb(ROOT),
            "last_error_line": state.get("last_error_line"),
        },
        "requests_ack": state.get("requests_ack") or {},
    }


def refresh_local_meta(state: dict, env: dict) -> None:
    ver = read_json(ROOT / "VERSION.json", {}) or {}
    state["code"] = {
        "version": ver.get("version"),
        "stale_code_running": bool(
            state.get("local_sha")
            and state.get("remote_sha")
            and state.get("local_sha") != state.get("remote_sha")
        ),
    }
    state["processes"] = {
        "mt5": process_info(["terminal64.exe", "terminal.exe"]),
        "bridge": process_info(["bridge_trader.py"]),
        "watchdog": process_info(["watchdog_exness.py"]),
        "connection_monitor": process_info(["connection_monitor.py"]),
        "github_agent": {"running": True, "pid": os.getpid(), "uptime_sec": None},
    }
    # Lightweight MT5 / desk hints from local bridge status if present.
    hb = read_json(STATE_DIR / "last_heartbeat_meta.json", {}) or {}
    state["mt5"] = {
        "login": hb.get("login") or env.get("MT5_LOGIN"),
        "symbol": hb.get("symbol") or env.get("MT5_SYMBOL") or "XAUUSDm",
        "tick_age_sec": hb.get("tick_age_sec"),
        "last_closed_bar": hb.get("last_closed_bar"),
        "mt5_connection": hb.get("mt5_connection"),
        "trade_allowed": hb.get("trade_allowed"),
    }
    state["desk_link"] = {
        "heartbeat_age_sec": hb.get("heartbeat_age_sec"),
        "frozen_tick": bool(
            (hb.get("tick_age_sec") or 0) > 60
            and (hb.get("heartbeat_age_sec") is not None)
            and (hb.get("heartbeat_age_sec") or 999) < 30
        ),
    }
    risk = read_json(STATE_DIR / "risk_snapshot.json", {}) or {}
    state["risk"] = {
        "n_positions": risk.get("n_positions"),
        "n_pending": risk.get("n_pending"),
    }


def apply_update(state: dict, env: dict) -> None:
    git_dir = Path(state["git_dir"])
    remote_url = state.get("remote_url")
    state["update_state"] = "waiting_close"
    state["apply_started_at"] = datetime.now(timezone.utc).isoformat()
    plan = candle_wait_plan()
    state["skip_bar_time"] = plan["skip_bar_time"]
    state["waited_close_bar"] = plan["waited_close_bar"]
    write_status(build_payload(state, env))
    post_status(env, build_payload(state, env))
    log(
        f"waiting candle close={plan['waited_close_bar']} "
        f"skip={plan['skip_bar_time']} sec={plan['wait_sec']}"
    )
    # Sleep in chunks so status/posts continue and restart can be deferred.
    deadline = time.time() + plan["wait_sec"]
    while time.time() < deadline:
        remaining = deadline - time.time()
        time.sleep(min(15, max(1, remaining)))
        write_status(build_payload(state, env))
        # Do not clear skip wait for restart — only note it.
        if state.get("pending_restart"):
            state["pending_restart_note"] = "deferred_until_after_apply"

    write_deploy_skip_marker(
        skip_bar_time=plan["skip_bar_time"],
        waited_close_bar=plan["waited_close_bar"],
        version=str((read_json(ROOT / "VERSION.json", {}) or {}).get("version") or "unknown"),
    )

    state["update_state"] = "applying"
    write_status(build_payload(state, env))
    post_status(env, build_payload(state, env))

    # Fetch latest into clone, then gate the tree before copying live runtime.
    try:
        if remote_url:
            subprocess.run(
                ["git", "remote", "set-url", "origin", remote_url],
                cwd=str(git_dir),
                timeout=30,
            )
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=str(git_dir),
            timeout=120,
            check=False,
        )
        subprocess.run(
            ["git", "pull", "--ff-only", "origin", "main"],
            cwd=str(git_dir),
            timeout=120,
            check=False,
        )
    except Exception as exc:
        state["update_state"] = "blocked"
        state["blocked_error"] = f"git fetch/pull failed: {exc}"
        state["last_result"] = "blocked"
        state["safety_gate"] = {"last_ok": False, "last_error": str(exc), "checked_sha": None}
        return

    tip = git_sha(git_dir, "HEAD")
    gate = run_safety_gate(git_dir)
    state["safety_gate"] = {
        "last_ok": bool(gate.get("ok")),
        "last_error": "; ".join(gate.get("errors") or []) or None,
        "checked_sha": tip,
    }
    if not gate.get("ok"):
        state["update_state"] = "blocked"
        state["blocked_error"] = state["safety_gate"]["last_error"]
        state["last_result"] = "update_blocked"
        RESULT_FILE.write_text(json.dumps({"ok": False, "gate": gate}, indent=2), encoding="utf-8")
        log(f"safety_gate FAIL: {state['blocked_error']}")
        return

    rc = invoke_auto_update(force=True, skip_wait=True)
    if rc != 0:
        state["update_state"] = "blocked"
        state["blocked_error"] = f"auto_update exit {rc}"
        state["last_result"] = "update_blocked"
        log(f"auto_update failed rc={rc}")
        return

    state["update_state"] = "restarting"
    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_restart_utc"] = now_iso
    state["local_sha"] = tip or state.get("remote_sha")
    state["last_result"] = "update_ok"
    state["last_successful_apply_utc"] = now_iso
    state["last_successful_apply_sha"] = tip
    state["blocked_error"] = None
    state["update_state"] = "idle"
    success_blob = {
        "ok": True,
        "sha": tip,
        "skip_bar_time": plan["skip_bar_time"],
        "waited_close_bar": plan["waited_close_bar"],
        "applied_at_utc": now_iso,
        "safety_gate_ok": True,
        "bridge_restarted": True,
    }
    RESULT_FILE.write_text(json.dumps(success_blob, indent=2), encoding="utf-8")
    (STATE_DIR / "last_successful_apply.json").write_text(
        json.dumps(success_blob, indent=2), encoding="utf-8"
    )
    log(f"update_ok sha={tip} skip={plan['skip_bar_time']}")

    if state.pop("pending_restart", None):
        restart_stack_safe(in_flight_wait=False)
        state["requests_ack"] = dict(state.get("requests_ack") or {})
        state["requests_ack"]["restart_stack"] = datetime.now(timezone.utc).isoformat()


def handle_commands(state: dict, env: dict, pending: list) -> None:
    ack = dict(state.get("requests_ack") or {})
    handled = list(ack.get("handled_ids") or [])
    in_flight = state.get("update_state") in ("waiting_close", "applying", "restarting")
    for cmd in pending or []:
        action = str(cmd.get("action") or "")
        cid = str(cmd.get("id") or "")
        if action == "force_check":
            state["force_check"] = True
            ack["force_check"] = datetime.now(timezone.utc).isoformat()
            ack["force_check_id"] = cid
            handled.append(cid)
            log(f"force_check queued id={cid}")
        elif action == "restart_stack":
            if in_flight:
                state["pending_restart"] = True
                note = restart_stack_safe(in_flight_wait=True)
                log(f"restart_stack deferred id={cid} note={note}")
            else:
                note = restart_stack_safe(in_flight_wait=False)
                log(f"restart_stack id={cid} note={note}")
            ack["restart_stack"] = datetime.now(timezone.utc).isoformat()
            ack["restart_stack_id"] = cid
            handled.append(cid)
    ack["handled_ids"] = handled[-32:]
    state["requests_ack"] = ack


def main() -> int:
    if not acquire_lock():
        log("another github_update_agent holds the lock; exiting")
        return 0
    env = load_env()
    git_dir = Path(
        env.get("BRIDGE_UPDATE_GIT")
        or os.environ.get("BRIDGE_UPDATE_GIT")
        or r"C:\onyxion-src\Latest_Gold_Trading_Bot"
    )
    remote_url = (
        env.get("BRIDGE_UPDATE_GIT_REMOTE")
        or os.environ.get("BRIDGE_UPDATE_GIT_REMOTE")
        or DEFAULT_REMOTE
    )
    state: dict = {
        "git_dir": str(git_dir),
        "remote_url": remote_url,
        "update_state": "idle",
        "last_result": None,
        "requests_ack": {},
    }
    prev_ok = read_json(STATE_DIR / "last_successful_apply.json", {}) or {}
    if prev_ok.get("applied_at_utc"):
        state["last_successful_apply_utc"] = prev_ok.get("applied_at_utc")
        state["last_successful_apply_sha"] = prev_ok.get("sha")
        if prev_ok.get("ok"):
            state["last_result"] = "update_ok"
    log(f"github_update_agent start root={ROOT} git={git_dir}")
    last_status = 0.0
    last_poll = 0.0

    while True:
        try:
            now = time.time()
            if now - last_poll >= POLL_SECONDS or state.get("force_check"):
                state["force_check"] = False
                state["last_poll_utc"] = datetime.now(timezone.utc).isoformat()
                meta = fetch_remote_commit_meta(remote_url)
                if meta.get("sha"):
                    state["remote_push_sha"] = meta.get("sha")
                    state["remote_push_utc"] = meta.get("pushed_at_utc")
                    state["remote_push_author"] = meta.get("author_login") or meta.get(
                        "author_name"
                    )
                    state["remote_push_message"] = meta.get("message")
                    # Prefer API tip when ls-remote fails.
                    if not state.get("remote_sha"):
                        state["remote_sha"] = meta.get("sha")
                    else:
                        state["remote_sha"] = meta.get("sha") or state.get("remote_sha")
                if git_dir.is_dir() and (git_dir / ".git").exists():
                    state["local_sha"] = git_sha(git_dir, "HEAD")
                    tip = remote_main_sha(git_dir, remote_url)
                    if tip:
                        state["remote_sha"] = tip
                else:
                    state["last_error_line"] = f"git dir missing: {git_dir}"
                    state["local_sha"] = None
                last_poll = now
                behind = (
                    state.get("local_sha")
                    and state.get("remote_sha")
                    and state["local_sha"] != state["remote_sha"]
                )
                if behind and state.get("update_state") == "idle":
                    apply_update(state, env)

            refresh_local_meta(state, env)
            payload = build_payload(state, env)
            write_status(payload)
            if now - last_status >= STATUS_SECONDS or state.get("update_state") != "idle":
                resp = post_status(env, payload)
                last_status = now
                if resp.get("ok"):
                    handle_commands(state, env, resp.get("pending_commands") or [])
                else:
                    state["last_error_line"] = str(resp.get("error") or "status post failed")

            time.sleep(5)
        except KeyboardInterrupt:
            log("stopped")
            return 0
        except Exception:
            err = traceback.format_exc()
            state["last_error_line"] = err.splitlines()[-1][:240]
            log(f"loop error: {err}")
            time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
