"""Keep the isolated Exness terminal and selected bridge alive on Windows."""

from __future__ import annotations

import subprocess
import sys
import time
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("MT5_ROOT", r"C:\onyxion"))
BRIDGE_MODEL = os.environ.get("MT5_BRIDGE_MODEL", "DEMO").strip().upper()
ENV_FILE = ROOT / ".env"


def env_value(name: str, default: str = "") -> str:
    """Read a simple KEY=value from the local, untracked .env file."""
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip() == name:
                    return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get(name, default)


INSTALL_ROOT = Path(
    env_value(
        f"{BRIDGE_MODEL}_MT5_INSTALL_ROOT",
        env_value("MT5_INSTALL_ROOT", str(ROOT / "exness-mt5")),
    )
)
TERMINAL = Path(
    env_value(
        f"{BRIDGE_MODEL}_MT5_TERMINAL_PATH",
        str(INSTALL_ROOT / "terminal64.exe"),
    )
)
BRIDGE = ROOT / "bridge_trader.py"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / f"exness_watchdog_{BRIDGE_MODEL.lower()}.log"
BRIDGE_LOG = LOG_DIR / f"bridge_{BRIDGE_MODEL.lower()}.log"
LOCK_FILE = ROOT / "state" / f"exness_watchdog_{BRIDGE_MODEL.lower()}.lock"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
POLL_SECONDS = 15
LOCK_HANDLE = None


def process_matches(name: str, fragment: str) -> bool:
    """Find a process by executable name and command-line fragment.

    The watchdog itself runs as SYSTEM and can be restarted independently of
    its children, so Popen handles alone are not enough to prevent duplicates.
    """
    command = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.Name -eq '{name}' -and "
        f"$_.CommandLine -like '*{fragment}*' }} | "
        "Select-Object -First 1 | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        output = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(output.strip())


def acquire_lock() -> None:
    global LOCK_HANDLE
    ROOT.joinpath("state").mkdir(parents=True, exist_ok=True)
    LOCK_HANDLE = LOCK_FILE.open("a+", encoding="utf-8")
    try:
        import msvcrt

        LOCK_HANDLE.seek(0)
        LOCK_HANDLE.write("1")
        LOCK_HANDLE.flush()
        LOCK_HANDLE.seek(0)
        msvcrt.locking(LOCK_HANDLE.fileno(), msvcrt.LK_NBLCK, 1)
    except (ImportError, OSError, IOError):
        LOCK_HANDLE.close()
        LOCK_HANDLE = None
        raise SystemExit("another Exness watchdog is already running")


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now(timezone.utc).isoformat()}] {message}\n"
    with LOG_FILE.open("a", encoding="utf-8") as stream:
        stream.write(line)


def main() -> int:
    if not TERMINAL.is_file():
        log(f"terminal missing: {TERMINAL}")
        return 2
    if not BRIDGE.is_file():
        log(f"bridge missing: {BRIDGE}")
        return 3

    acquire_lock()
    log("Exness watchdog started")
    try:
        while True:
            if not process_matches("terminal64.exe", str(INSTALL_ROOT)):
                subprocess.Popen(
                    [str(TERMINAL), "/portable"],
                    cwd=str(INSTALL_ROOT),
                    creationflags=CREATE_NO_WINDOW,
                )
                log("started dedicated MT5 terminal")
                time.sleep(30)

            if not process_matches("python.exe", "bridge_trader.py"):
                bridge_stream = BRIDGE_LOG.open("a", encoding="utf-8")
                subprocess.Popen(
                    [sys.executable, str(BRIDGE), "--model", BRIDGE_MODEL],
                    cwd=str(ROOT),
                    stdout=bridge_stream,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                )
                log(f"started {BRIDGE_MODEL} bridge")
                # The child owns this file descriptor after Popen; closing
                # our handle avoids leaking one descriptor per restart.
                bridge_stream.close()

            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        log("watchdog stopped")
        return 0
    finally:
        if LOCK_HANDLE is not None:
            LOCK_HANDLE.close()


if __name__ == "__main__":
    raise SystemExit(main())
