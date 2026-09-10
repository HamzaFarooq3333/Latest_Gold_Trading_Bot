"""Connection-health layer for the Ali PC MT5 -> GCP live bridge.

watchdog_exness.py keeps the *processes* alive. This monitor answers the
different question of whether those processes are still doing anything useful:
MT5 can sit there running while its broker session is dead, or while the PC has
no internet, and the process supervisor would never notice.

Every 5 minutes it records CONNECTED / DISCONNECTED / RESTARTED. MT5 is only
restarted after two consecutive failed checks (~10 minutes), so a brief broker
blip or a router reboot does not cause a pointless terminal bounce.

Logs (all timestamps UTC, one file per concern):
  logs/connection_check.log  - every cycle: process alive, broker session, net
  logs/data_heartbeat.log    - every 15 min: did our data actually reach GCP
  logs/errors.log            - any exception, with full traceback
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("MT5_ROOT", r"C:\onyxion-ali"))
ENV_FILE = ROOT / ".env"
LOG_DIR = ROOT / "logs"
CONN_LOG = LOG_DIR / "connection_check.log"
BEAT_LOG = LOG_DIR / "data_heartbeat.log"
ERR_LOG = LOG_DIR / "errors.log"
LOCK_FILE = ROOT / "state" / "connection_monitor.lock"

CHECK_SECONDS = 300           # 5-minute connection check
HEARTBEAT_LOG_SECONDS = 900   # 15-minute data-delivery confirmation
FAILURES_BEFORE_RESTART = 2   # ~10 minutes down before we bounce MT5
HEARTBEAT_STALE_SECONDS = 120  # desk heartbeat older than this = not arriving

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


def _write(path: Path, line: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def log_conn(status: str, msg: str) -> None:
    line = f"{_stamp()} UTC [{status}] {msg}"
    _write(CONN_LOG, line)
    # Under pythonw.exe / a hidden Scheduled Task there is no stdout to write
    # to; the log file is the real output, so never let this kill the monitor.
    try:
        print(line, flush=True)
    except Exception:
        pass


def log_beat(status: str, msg: str) -> None:
    _write(BEAT_LOG, f"{_stamp()} UTC [{status}] {msg}")


def log_err(context: str, exc: BaseException) -> None:
    _write(ERR_LOG, f"{_stamp()} UTC [ERROR] {context}: {exc!r}")
    _write(ERR_LOG, traceback.format_exc().rstrip())


def acquire_lock() -> None:
    """Only one monitor at a time — the task repeats every 5 min to self-heal."""
    global LOCK_HANDLE
    (ROOT / "state").mkdir(parents=True, exist_ok=True)
    LOCK_HANDLE = LOCK_FILE.open("a+", encoding="utf-8")
    try:
        import msvcrt

        msvcrt.locking(LOCK_HANDLE.fileno(), msvcrt.LK_NBLCK, 1)
    except (ImportError, OSError, IOError):
        LOCK_HANDLE.close()
        LOCK_HANDLE = None
        # The 5-minute task deliberately re-runs this; an already-running
        # monitor is the healthy case, so exit 0 and keep task history clean.
        raise SystemExit(0)


def terminal_running(install_root: Path) -> int | None:
    """PID of our portable terminal, or None. Matched on path so a personal
    MT5 install elsewhere on the PC is never mistaken for this one."""
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{install_root}*' }} | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).strip()
        return int(out) if out.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def internet_ok(lab_url: str) -> bool:
    """Distinguishes 'MT5 died' from 'the internet is down' in the logs."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # GET, not HEAD: the desk answers 405 to HEAD, which would read as an
    # outage every cycle and mask real connectivity loss.
    try:
        req = urllib.request.Request(
            lab_url.rstrip("/") + "/api/broker/state",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15, context=ctx):
            return True
    except Exception:
        return False


def broker_session_live(terminal_path: str) -> tuple[bool, str]:
    """Attach to the already-running terminal and ask whether the broker
    session is actually up.

    Deliberately attaches WITHOUT credentials: passing login/password here
    would force a re-login and could disturb the bridge's own MT5 session.
    """
    try:
        import MetaTrader5 as mt5
    except Exception as exc:
        return False, f"MetaTrader5 package unavailable: {exc}"

    try:
        if not mt5.initialize(terminal_path, portable=True, timeout=60000):
            return False, f"initialize failed {mt5.last_error()}"
        try:
            info = mt5.terminal_info()
            acct = mt5.account_info()
            if info is None:
                return False, "terminal_info None"
            if not getattr(info, "connected", False):
                return False, "terminal reports not connected to broker"
            if acct is None:
                return False, f"account_info None {mt5.last_error()}"
            return True, (
                f"login={acct.login} server={acct.server} "
                f"balance={acct.balance} trade_allowed={acct.trade_allowed} "
                f"terminal_trade_allowed={getattr(info, 'trade_allowed', None)}"
            )
        finally:
            mt5.shutdown()
    except Exception as exc:
        return False, f"exception {exc!r}"


def desk_heartbeat(lab_url: str) -> tuple[bool, str]:
    """Confirm our data is landing on the GCP desk, not just that we sent it."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(
            lab_url.rstrip("/") + "/api/broker/state",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return False, f"desk unreachable: {exc}"

    broker = data.get("broker") or {}
    bridge = broker.get("bridge") or {}
    age = broker.get("heartbeat_age_sec")
    acct = broker.get("account") or {}
    detail = (
        f"heartbeat_age={age}s login={acct.get('login')} "
        f"symbol={broker.get('symbol')} lot={bridge.get('lot')} "
        f"mt5={bridge.get('mt5_connection')} last_bar={bridge.get('last_closed_bar')} "
        f"last_tick={bridge.get('tick_time')}"
    )
    if age is None or age > HEARTBEAT_STALE_SECONDS:
        return False, detail
    return True, detail


def stale_code_running() -> tuple[bool, str]:
    """True when bridge_trader.py on disk is newer than the process running it.

    auto_update_bridge.ps1 syncs new code and then restarts the bridge. If that
    restart fails - and it does, because the sync strips the UTF-8 BOM off
    scripts\\*.ps1 and PowerShell 5.1 then cannot parse start_bridge_stack.ps1 -
    the old process keeps running the old code indefinitely while the files on
    disk look correct. Version drift is the only reliable signal for that.
    """
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*bridge_trader.py*' } | "
        "Select-Object -First 1 -ExpandProperty CreationDate | "
        "ForEach-Object { $_.ToUniversalTime().ToString('o') }"
    )
    try:
        out = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            text=True, stderr=subprocess.DEVNULL, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).strip()
        if not out:
            return False, "bridge not running"
        started = datetime.fromisoformat(out).timestamp()
    except Exception as exc:
        return False, f"could not read bridge start time: {exc}"

    newest = 0.0
    newest_name = ""
    for name in ("bridge_trader.py", "mt5_live_engine.py", "VERSION.json"):
        f = ROOT / name
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if m > newest:
            newest, newest_name = m, name

    # 60s grace: the bridge writes nothing here, but a sync landing in the same
    # minute as a restart should not be mistaken for drift.
    if newest > started + 60:
        return True, (
            f"{newest_name} modified after bridge started "
            f"({datetime.fromtimestamp(newest, timezone.utc):%H:%M:%S} > "
            f"{datetime.fromtimestamp(started, timezone.utc):%H:%M:%S} UTC)"
        )
    return False, "code and process in sync"


def restart_bridge_for_new_code(detail: str) -> None:
    """Stop watchdog+bridge so the next start picks the new code off disk."""
    log_conn("STALE_CODE", f"restarting bridge: {detail}")
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*watchdog_exness.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
        "Start-Sleep -Seconds 4; "
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*bridge_trader.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            text=True, capture_output=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log_err("restart_bridge_for_new_code", exc)
    # Repair script encoding before starting, or the start will fail the same
    # way the auto-update's own restart just did.
    repair = ROOT / "fix_script_encoding.ps1"
    if repair.is_file():
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-File", str(repair), "-Root", str(ROOT)],
                text=True, capture_output=True, timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            log_err("encoding repair", exc)
    ensure_bridge_stack()
    log_conn("STALE_CODE", "bridge restarted on new code")


def restart_mt5() -> str:
    script = ROOT / "scripts" / "launch_mt5.ps1"
    try:
        proc = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-Root", str(ROOT), "-Force",
            ],
            text=True, capture_output=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (proc.stdout or proc.stderr or "").strip().replace("\n", " | ")
    except Exception as exc:
        log_err("restart_mt5", exc)
        return f"restart failed: {exc}"


def ensure_bridge_stack() -> None:
    """The watchdog supervises the bridge, but nothing supervises the watchdog.
    Re-running the start script is a no-op when it is already up."""
    script = ROOT / "scripts" / "start_bridge_stack.ps1"
    try:
        subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-Profile", "ali", "-Root", str(ROOT),
            ],
            text=True, capture_output=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log_err("ensure_bridge_stack", exc)


def main() -> int:
    acquire_lock()
    env = load_env()
    lab_url = env.get("ASIM_LAB_URL", "https://35.253.21.246")
    terminal_path = env.get(
        "ASIM_MT5_TERMINAL_PATH", str(ROOT / "exness-mt5" / "terminal64.exe")
    )
    install_root = str(Path(terminal_path).parent)

    log_conn("START", f"connection monitor started root={ROOT} desk={lab_url}")
    consecutive_failures = 0
    last_beat_log = 0.0

    while True:
        try:
            net = internet_ok(lab_url)
            pid = terminal_running(Path(install_root))

            if pid is None:
                consecutive_failures += 1
                log_conn(
                    "DISCONNECTED",
                    f"MT5 process not running (net={'UP' if net else 'DOWN'}) "
                    f"failures={consecutive_failures}/{FAILURES_BEFORE_RESTART}",
                )
            else:
                live, detail = broker_session_live(terminal_path)
                if live:
                    consecutive_failures = 0
                    log_conn(
                        "CONNECTED",
                        f"pid={pid} net={'UP' if net else 'DOWN'} {detail}",
                    )
                else:
                    consecutive_failures += 1
                    log_conn(
                        "DISCONNECTED",
                        f"pid={pid} net={'UP' if net else 'DOWN'} {detail} "
                        f"failures={consecutive_failures}/{FAILURES_BEFORE_RESTART}",
                    )

            # Only bounce the terminal once the failure has persisted, and never
            # blame MT5 for what is really an internet outage.
            if consecutive_failures >= FAILURES_BEFORE_RESTART:
                if not net and pid is not None:
                    log_conn(
                        "WAITING",
                        "internet DOWN and MT5 alive — holding off restart until "
                        "connectivity returns",
                    )
                else:
                    out = restart_mt5()
                    log_conn("RESTARTED", f"MT5 relaunched with auto-login: {out}")
                    consecutive_failures = 0

            ensure_bridge_stack()

            # New code on disk that the running bridge never loaded is a silent
            # failure: heartbeats keep flowing and every health check passes
            # while the old logic is still trading.
            stale, why = stale_code_running()
            if stale:
                restart_bridge_for_new_code(why)

            now = time.time()
            if now - last_beat_log >= HEARTBEAT_LOG_SECONDS:
                ok, detail = desk_heartbeat(lab_url)
                log_beat("SENT" if ok else "SEND_FAILED", detail)
                last_beat_log = now

        except Exception as exc:  # never let the monitor die on a bad cycle
            log_err("monitor cycle", exc)
            log_conn("ERROR", f"cycle failed: {exc!r}")

        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        log_conn("STOP", "connection monitor stopped")
    except Exception as exc:
        log_err("fatal", exc)
        raise
