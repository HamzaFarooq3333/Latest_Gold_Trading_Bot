"""Candle-safe GitHub update agent for Ali PC.

Polls origin/main of Latest_Gold_Trading_Bot (every POLL_SECONDS). When the
clone under BRIDGE_UPDATE_GIT is behind: wait for the current M15 candle to
close, write state/deploy_skip_bar.json (the bridge takes no NEW entries on
the next candle - open tickets and stops are untouched), run the safety gate
on the pulled tree, then apply via scripts/auto_update_bridge.ps1 which
copies the runtime and restarts the bridge stack.

Posts a status blob to the GCP desk every STATUS_SECONDS
(POST /api/broker/ali_pc_status) and executes the desk's queued commands
(force_check / restart_stack). Never redeploys the desk itself.
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
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows: every child this agent spawns (git, powershell) would open a
# console window that flashes to the foreground even under pythonw.exe.
# CREATE_NO_WINDOW is forced on every subprocess entry point so a call site
# added later cannot reintroduce the popups.
if os.name == "nt":
    _CREATE_NO_WINDOW = 0x08000000

    def _silence_console(fn):
        def _wrapped(*args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
            return fn(*args, **kwargs)
        return _wrapped

    for _name in ("run", "check_output", "check_call", "call", "Popen"):
        setattr(subprocess, _name, _silence_console(getattr(subprocess, _name)))

ROOT = Path(os.environ.get("MT5_ROOT", r"C:\onyxion-ali"))
ENV_FILE = ROOT / ".env"
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"
STATUS_FILE = STATE_DIR / "github_agent_status.json"
RESULT_FILE = STATE_DIR / "last_update_result.json"
SUCCESS_FILE = STATE_DIR / "last_successful_apply.json"
SKIP_FILE = STATE_DIR / "deploy_skip_bar.json"
BRIDGE_STATUS_FILE = STATE_DIR / "bridge_status.json"
LOCK_FILE = STATE_DIR / "github_update_agent.lock"
AGENT_LOG = LOG_DIR / "github_update_agent.log"

POLL_SECONDS = int(os.environ.get("GITHUB_AGENT_POLL_SEC", "90"))
STATUS_SECONDS = int(os.environ.get("GITHUB_AGENT_STATUS_SEC", "20"))
LOOP_SECONDS = 5
DEFAULT_REMOTE = "https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git"
APPLY_STATES = ("waiting_close", "applying", "restarting")

LOCK_HANDLE = None
_BOOT_TIME: datetime | None = None
_PROCESS_CACHE: dict = {"at": 0.0, "value": {}}


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{_now():%Y-%m-%d %H:%M:%S} UTC {msg}"
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


# ---- candle timing -----------------------------------------------------------

def m15_open_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    return now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)


def candle_wait_plan(now: datetime | None = None) -> dict:
    """Current M15 open, its close, the bar to skip, and how long to wait."""
    now = now or _now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    current_open = m15_open_utc(now)
    close_at = current_open + timedelta(minutes=15)
    wait_sec = min(960, max(3, int((close_at - now).total_seconds()) + 3))
    return {"waited_close_bar": current_open.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "skip_bar_time": close_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "wait_sec": wait_sec, "close_at_utc": close_at.strftime("%Y-%m-%dT%H:%M:%SZ")}


def write_deploy_skip_marker(*, skip_bar_time: str, waited_close_bar: str, version: str = "unknown") -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SKIP_FILE.write_text(json.dumps({
        "skip_bar_time": skip_bar_time, "waited_close_bar": waited_close_bar, "version": version,
        "reason": "github_agent_skip_next_bar", "no_flatten": True, "preserve_positions": True,
        "preserve_sl": True, "written_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, indent=2), encoding="utf-8")
    return SKIP_FILE


# ---- small io ----------------------------------------------------------------

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
    blob["written_at"] = _now().isoformat()
    STATUS_FILE.write_text(json.dumps(blob, indent=2), encoding="utf-8")


def _git(args: list[str], cwd: Path, timeout: int = 60) -> str | None:
    try:
        return subprocess.check_output(["git", *args], cwd=str(cwd), stderr=subprocess.DEVNULL,
                                       text=True, timeout=timeout).strip() or None
    except Exception:
        return None


def git_sha(cwd: Path, ref: str = "HEAD") -> str | None:
    return _git(["rev-parse", ref], cwd, 30)


def remote_main_sha(git_dir: Path, remote_url: str | None) -> str | None:
    if remote_url:
        subprocess.run(["git", "remote", "set-url", "origin", remote_url], cwd=str(git_dir),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    out = _git(["ls-remote", "origin", "refs/heads/main"], git_dir, 60)
    return out.split()[0] if out else None


def process_info(patterns: list[str]) -> dict:
    """Windows process lookup by command-line substring (one PowerShell call)."""
    info = {"running": False, "pid": None, "uptime_sec": None}
    if sys.platform != "win32":
        return info
    try:
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
               "$p = Get-CimInstance Win32_Process | Where-Object { "
               + " -or ".join(f"$_.CommandLine -like '*{pat}*'" for pat in patterns)
               + " } | Select-Object -First 1 ProcessId,CreationDate; "
               "if ($p) { $age = [int]((Get-Date) - $p.CreationDate).TotalSeconds; "
               "Write-Output ($p.ProcessId.ToString() + '|' + $age) }"]
        out = subprocess.check_output(cmd, text=True, timeout=20, stderr=subprocess.DEVNULL).strip()
        if "|" in out:
            pid_s, age_s = out.split("|", 1)
            info.update(running=True, pid=int(pid_s), uptime_sec=int(age_s))
    except Exception:
        pass
    return info


def all_process_info() -> dict:
    """Process table for the desk panel, refreshed at most every STATUS_SECONDS.

    Each lookup spawns PowerShell; doing four of them every 5 s loop tick
    (as before) cost ~50 process launches a minute for numbers the desk only
    reads every 20 s.
    """
    now = time.time()
    if now - _PROCESS_CACHE["at"] < STATUS_SECONDS and _PROCESS_CACHE["value"]:
        return _PROCESS_CACHE["value"]
    value = {
        "mt5": process_info(["terminal64.exe"]),
        "bridge": process_info(["bridge_trader.py"]),
        "watchdog": process_info(["watchdog_exness.py"]),
        "connection_monitor": process_info(["connection_monitor.py"]),
        "github_agent": {"running": True, "pid": os.getpid(), "uptime_sec": None},
    }
    _PROCESS_CACHE.update(at=now, value=value)
    return value


def pc_uptime_sec() -> int | None:
    """Boot time is read once; uptime is then plain arithmetic."""
    global _BOOT_TIME
    if sys.platform != "win32":
        return None
    if _BOOT_TIME is None:
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')"],
                text=True, timeout=15).strip()
            boot = datetime.fromisoformat(out.replace("Z", "+00:00"))
            _BOOT_TIME = boot if boot.tzinfo else boot.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return int((_now() - _BOOT_TIME).total_seconds())


def disk_free_gb(path: Path) -> float | None:
    try:
        return round(shutil.disk_usage(str(path)).free / (1024 ** 3), 2)
    except Exception:
        return None


def run_safety_gate(check_root: Path) -> dict:
    checker = ROOT / "safety_gate_check.py"
    try:
        proc = subprocess.run([sys.executable, str(checker), "--root", str(check_root), "--json", "--no-dashboard"],
                              capture_output=True, text=True, timeout=120)
        raw = (proc.stdout or "").strip()
        data = json.loads(raw) if raw.startswith("{") else {"ok": proc.returncode == 0, "errors": [raw or proc.stderr]}
        data["returncode"] = proc.returncode
        return data
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)], "returncode": 1}


def _powershell_script(name: str, *args: str, timeout: int) -> int:
    script = ROOT / "scripts" / name
    if not script.is_file():
        log(f"missing {script}")
        return 1
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
                              cwd=str(ROOT), timeout=timeout)
        return int(proc.returncode)
    except Exception as exc:
        log(f"{name} error: {exc}")
        return 1


def invoke_auto_update(*, force: bool = True, skip_wait: bool = False) -> int:
    args = ["-Profile", "ali", "-Root", str(ROOT)]
    if force:
        args.append("-Force")
    if skip_wait:
        args.append("-SkipCandleWait")
    return _powershell_script("auto_update_bridge.ps1", *args, timeout=1200)


def restart_stack_safe(*, in_flight_wait: bool) -> dict:
    """Restart the bridge stack without aborting an in-flight candle wait."""
    if in_flight_wait:
        return {"ok": True, "deferred": True, "note": "restart queued after candle wait (did not abort skip wait)"}
    rc = _powershell_script("start_bridge_stack.ps1", "-Profile", "ali", "-Root", str(ROOT), timeout=180)
    return {"ok": rc == 0, "deferred": False, "note": f"start_bridge_stack exit {rc}"}


def post_status(env: dict, payload: dict) -> dict:
    base = (env.get("ASIM_LAB_URL") or os.environ.get("ASIM_LAB_URL") or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "ASIM_LAB_URL missing"}
    req = urllib.request.Request(base + "/api/broker/ali_pc_status", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "User-Agent": "onyxion-github-agent"},
                                 method="POST")
    ctx = ssl.create_default_context()
    if (env.get("ASIM_LAB_INSECURE") or "1").strip().lower() not in ("0", "false", "no"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def fetch_remote_commit_meta(remote_url: str | None) -> dict:
    """Tip metadata from the GitHub API (public repo, no token)."""
    repo = "HamzaFarooq3333/Latest_Gold_Trading_Bot"
    if remote_url and "github.com" in remote_url:
        part = remote_url.rstrip("/").split("github.com/")[-1].replace(".git", "").strip("/")
        if part.count("/") == 1:
            repo = part
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/commits/main",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "onyxion-github-agent"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        commit = data.get("commit") or {}
        author = commit.get("author") or {}
        return {"sha": data.get("sha"), "pushed_at_utc": author.get("date"),
                "author_name": author.get("name") or (data.get("author") or {}).get("login"),
                "author_login": (data.get("author") or {}).get("login"),
                "message": ((commit.get("message") or "").splitlines() or [""])[0][:160],
                "html_url": data.get("html_url"), "repo": repo}
    except Exception as exc:
        return {"error": str(exc), "repo": repo}


# ---- desk payload ------------------------------------------------------------

def yes_no(flag: bool | None) -> str:
    return "YES" if flag is True else "NO" if flag is False else "UNKNOWN"


def build_sync_timeline(state: dict) -> dict:
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
        "ali_detected_github_change": yes_no(behind), "ali_detected_github_change_bool": behind,
        "ali_started_clone_and_apply": yes_no(upd in APPLY_STATES),
        "ali_started_clone_and_apply_bool": upd in APPLY_STATES,
        "apply_phase": upd, "apply_started_at_utc": state.get("apply_started_at"),
        "last_successful_pull_test_restart_utc": last_ok,
        "last_successful_apply_sha": state.get("last_successful_apply_sha"),
        "last_successful_apply_result": last_result if last_result == "update_ok" else ("update_ok" if last_ok else last_result),
        "bridge_in_sync_with_github": yes_no(bool(local and remote and local == remote)),
        "notes": "Desk/GCP redeploy is Hamza-only. Ali PC only pulls bridge/runtime (candle-safe; never flattens; never runs install.sh).",
    }


def build_payload(state: dict, env: dict) -> dict:
    local, remote = state.get("local_sha"), state.get("remote_sha")
    timeline = build_sync_timeline(state)
    return {
        "host": os.environ.get("COMPUTERNAME") or "ali-pc", "profile": "ali",
        "github_checker": {
            "running": True, "last_poll_utc": state.get("last_poll_utc"),
            "local_sha": local, "remote_sha": remote, "behind": bool(local and remote and local != remote),
            "git_dir": state.get("git_dir"), "remote": state.get("remote_url"),
            "last_github_push_utc": state.get("remote_push_utc"),
            "last_github_push_by": state.get("remote_push_author"),
            "last_github_push_message": state.get("remote_push_message"),
            "ali_detected_change": timeline["ali_detected_github_change"],
        },
        "update": {
            "state": state.get("update_state", "idle"), "last_result": state.get("last_result"),
            "skip_bar_time": state.get("skip_bar_time"), "waited_close_bar": state.get("waited_close_bar"),
            "last_restart_utc": state.get("last_restart_utc"), "blocked_error": state.get("blocked_error"),
            "apply_started_at_utc": state.get("apply_started_at"),
            "last_successful_pull_test_restart_utc": state.get("last_successful_apply_utc"),
            "ali_started_clone_and_apply": timeline["ali_started_clone_and_apply"],
        },
        "sync_timeline": timeline,
        "processes": state.get("processes") or {}, "mt5": state.get("mt5") or {},
        "desk_link": state.get("desk_link") or {}, "code": state.get("code") or {},
        "risk": state.get("risk") or {}, "safety_gate": state.get("safety_gate") or {},
        "pc": {"uptime_sec": pc_uptime_sec(), "disk_free_gb": disk_free_gb(ROOT),
               "last_error_line": state.get("last_error_line")},
        "requests_ack": state.get("requests_ack") or {},
    }


def refresh_local_meta(state: dict, env: dict) -> None:
    ver = read_json(ROOT / "VERSION.json", {}) or {}
    state["code"] = {
        "version": ver.get("version"),
        "behind_github": bool(state.get("local_sha") and state.get("remote_sha")
                              and state.get("local_sha") != state.get("remote_sha")),
    }
    state["processes"] = all_process_info()
    # Written by the bridge on every heartbeat (state/bridge_status.json).
    hb = read_json(BRIDGE_STATUS_FILE, {}) or {}
    written = hb.get("written_at")
    hb_age = None
    if written:
        try:
            hb_age = int((_now() - datetime.fromisoformat(str(written).replace("Z", "+00:00"))).total_seconds())
        except ValueError:
            hb_age = None
    state["mt5"] = {
        "login": hb.get("login") or env.get("ASIM_MT5_LOGIN"),
        "symbol": hb.get("symbol") or env.get("ASIM_MT5_SYMBOL") or "XAUUSDm",
        "tick_age_sec": hb.get("tick_age_sec"), "last_closed_bar": hb.get("last_closed_bar"),
        "mt5_connection": hb.get("mt5_connection"), "trade_allowed": hb.get("trade_allowed"),
        "terminal_trade_allowed": hb.get("terminal_trade_allowed"), "engine_mode": hb.get("engine_mode"),
        "lot": hb.get("lot"), "controls_source": hb.get("live_controls_source"),
    }
    state["desk_link"] = {
        "bridge_status_age_sec": hb_age, "last_heartbeat_ok": hb.get("heartbeat_ok"),
        # Ticks stop while the desk still gets heartbeats = MT5 feed frozen.
        "frozen_tick": bool((hb.get("tick_age_sec") or 0) > 60 and hb.get("heartbeat_ok") and (hb_age or 999) < 60),
    }
    state["risk"] = {"n_positions": hb.get("n_positions"), "pending_bar_time": hb.get("pending_bar_time")}


# ---- apply -------------------------------------------------------------------

def _block(state: dict, error: str, result: str = "update_blocked") -> None:
    state["update_state"] = "blocked"
    state["blocked_error"] = error
    state["last_result"] = result
    log(f"update blocked: {error}")


def apply_update(state: dict, env: dict) -> None:
    git_dir = Path(state["git_dir"])
    remote_url = state.get("remote_url")
    state["update_state"] = "waiting_close"
    state["apply_started_at"] = _now().isoformat()
    plan = candle_wait_plan()
    state["skip_bar_time"], state["waited_close_bar"] = plan["skip_bar_time"], plan["waited_close_bar"]
    write_status(build_payload(state, env))
    post_status(env, build_payload(state, env))
    log(f"waiting candle close={plan['waited_close_bar']} skip={plan['skip_bar_time']} sec={plan['wait_sec']}")
    deadline = time.time() + plan["wait_sec"]
    last_wait_post = time.time()
    while time.time() < deadline:
        time.sleep(min(15, max(1, deadline - time.time())))
        write_status(build_payload(state, env))
        # Keep desk Ali-PC panel online during candle wait (desk redeploy
        # wipes ali_pc; without posts the UI shows AGENT OFFLINE for >90s).
        if time.time() - last_wait_post >= STATUS_SECONDS:
            post_status(env, build_payload(state, env))
            last_wait_post = time.time()

    write_deploy_skip_marker(skip_bar_time=plan["skip_bar_time"], waited_close_bar=plan["waited_close_bar"],
                             version=str((read_json(ROOT / "VERSION.json", {}) or {}).get("version") or "unknown"))
    state["update_state"] = "applying"
    write_status(build_payload(state, env))
    post_status(env, build_payload(state, env))

    try:
        if remote_url:
            subprocess.run(["git", "remote", "set-url", "origin", remote_url], cwd=str(git_dir), timeout=30)
        subprocess.run(["git", "fetch", "origin", "main"], cwd=str(git_dir), timeout=120, check=False)
        subprocess.run(["git", "pull", "--ff-only", "origin", "main"], cwd=str(git_dir), timeout=120, check=False)
    except Exception as exc:
        state["safety_gate"] = {"last_ok": False, "last_error": str(exc), "checked_sha": None}
        _block(state, f"git fetch/pull failed: {exc}", "blocked")
        return

    tip = git_sha(git_dir, "HEAD")
    gate = run_safety_gate(git_dir)
    state["safety_gate"] = {"last_ok": bool(gate.get("ok")),
                            "last_error": "; ".join(gate.get("errors") or []) or None, "checked_sha": tip}
    if not gate.get("ok"):
        RESULT_FILE.write_text(json.dumps({"ok": False, "gate": gate}, indent=2), encoding="utf-8")
        _block(state, state["safety_gate"]["last_error"] or "safety gate failed")
        return

    rc = invoke_auto_update(force=True, skip_wait=True)
    if rc != 0:
        _block(state, f"auto_update exit {rc}")
        return

    now_iso = _now().isoformat()
    state.update(update_state="idle", last_restart_utc=now_iso, local_sha=tip or state.get("remote_sha"),
                 last_result="update_ok", last_successful_apply_utc=now_iso, last_successful_apply_sha=tip,
                 blocked_error=None)
    blob = {"ok": True, "sha": tip, "skip_bar_time": plan["skip_bar_time"],
            "waited_close_bar": plan["waited_close_bar"], "applied_at_utc": now_iso,
            "safety_gate_ok": True, "bridge_restarted": True}
    RESULT_FILE.write_text(json.dumps(blob, indent=2), encoding="utf-8")
    SUCCESS_FILE.write_text(json.dumps(blob, indent=2), encoding="utf-8")
    log(f"update_ok sha={tip} skip={plan['skip_bar_time']}")
    if state.pop("pending_restart", None):
        restart_stack_safe(in_flight_wait=False)
        state["requests_ack"] = dict(state.get("requests_ack") or {})
        state["requests_ack"]["restart_stack"] = _now().isoformat()


def flatten_positions_safe() -> dict:
    """Close all open Onyxion magic positions via MT5 (desk-requested flatten)."""
    script = ROOT / "flatten_open_positions.py"
    if not script.is_file():
        return {"ok": False, "error": f"missing {script}"}
    py = sys.executable or "python"
    try:
        proc = subprocess.run(
            [py, str(script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()[-20:]
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "log_tail": tail,
    }


def handle_commands(state: dict, env: dict, pending: list) -> None:
    ack = dict(state.get("requests_ack") or {})
    handled = list(ack.get("handled_ids") or [])
    in_flight = state.get("update_state") in APPLY_STATES
    for cmd in pending or []:
        action, cid = str(cmd.get("action") or ""), str(cmd.get("id") or "")
        if action == "force_check":
            state["force_check"] = True
            ack["force_check"], ack["force_check_id"] = _now().isoformat(), cid
            handled.append(cid)
            log(f"force_check queued id={cid}")
        elif action == "restart_stack":
            if in_flight:
                state["pending_restart"] = True
            note = restart_stack_safe(in_flight_wait=in_flight)
            log(f"restart_stack id={cid} note={note}")
            ack["restart_stack"], ack["restart_stack_id"] = _now().isoformat(), cid
            handled.append(cid)
        elif action == "flatten":
            note = flatten_positions_safe()
            log(f"flatten id={cid} note={note}")
            ack["flatten"], ack["flatten_id"] = _now().isoformat(), cid
            ack["flatten_result"] = note
            handled.append(cid)
    ack["handled_ids"] = handled[-32:]
    state["requests_ack"] = ack


def main() -> int:
    if not acquire_lock():
        log("another github_update_agent holds the lock; exiting")
        return 0
    env = load_env()
    git_dir = Path(env.get("BRIDGE_UPDATE_GIT") or os.environ.get("BRIDGE_UPDATE_GIT") or r"C:\onyxion-src\Latest_Gold_Trading_Bot")
    remote_url = env.get("BRIDGE_UPDATE_GIT_REMOTE") or os.environ.get("BRIDGE_UPDATE_GIT_REMOTE") or DEFAULT_REMOTE
    state: dict = {"git_dir": str(git_dir), "remote_url": remote_url, "update_state": "idle",
                   "last_result": None, "requests_ack": {}}
    prev_ok = read_json(SUCCESS_FILE, {}) or {}
    if prev_ok.get("applied_at_utc"):
        state["last_successful_apply_utc"] = prev_ok.get("applied_at_utc")
        state["last_successful_apply_sha"] = prev_ok.get("sha")
        if prev_ok.get("ok"):
            state["last_result"] = "update_ok"
    log(f"github_update_agent start root={ROOT} git={git_dir}")
    last_status = last_poll = 0.0

    while True:
        try:
            now = time.time()
            if now - last_poll >= POLL_SECONDS or state.get("force_check"):
                state["force_check"] = False
                state["last_poll_utc"] = _now().isoformat()
                meta = fetch_remote_commit_meta(remote_url)
                if meta.get("sha"):
                    state.update(remote_push_sha=meta["sha"], remote_push_utc=meta.get("pushed_at_utc"),
                                 remote_push_author=meta.get("author_login") or meta.get("author_name"),
                                 remote_push_message=meta.get("message"), remote_sha=meta["sha"])
                if (git_dir / ".git").exists():
                    state["local_sha"] = git_sha(git_dir, "HEAD")
                    tip = remote_main_sha(git_dir, remote_url)
                    if tip:
                        state["remote_sha"] = tip
                else:
                    state["last_error_line"] = f"git dir missing: {git_dir}"
                    state["local_sha"] = None
                last_poll = now
                behind = state.get("local_sha") and state.get("remote_sha") and state["local_sha"] != state["remote_sha"]
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
            time.sleep(LOOP_SECONDS)
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
