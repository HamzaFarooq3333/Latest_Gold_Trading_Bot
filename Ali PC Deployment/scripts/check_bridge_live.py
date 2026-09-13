"""Decide whether the Onyxion bridge is genuinely live.

A running python.exe is NOT proof of life: the bridge has previously sat in a
restart loop (ModuleNotFoundError) with a process present the whole time while
no data reached the desk. The authoritative test is therefore the desk
heartbeat age, with the local process only used to tell "still starting up"
apart from "dead".

Exit codes (consumed by START_BOT.bat):
    0  LIVE        heartbeat is fresh -- do nothing
    1  NOT LIVE    start the stack
    2  STARTING    process is up and young; give it a moment, do not restart
"""

from __future__ import annotations

import json
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
FRESH_SECONDS = 60          # heartbeat older than this = not flowing
STARTUP_GRACE_SECONDS = 180  # a just-started bridge deserves time to connect


def read_env(name: str, default: str = "") -> str:
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip() == name:
                    return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return default


def heartbeat_age(lab_url: str) -> float | None:
    """Seconds since the desk last received data, or None if unreachable."""
    ctx = ssl.create_default_context()
    # The GCP desk serves a self-signed certificate; the bridge sets
    # ASIM_LAB_INSECURE=1 for the same reason.
    if read_env("ASIM_LAB_INSECURE", "1").lower() in ("1", "true", "yes", "on"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    url = lab_url.rstrip("/") + "/api/broker/state"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "onyxion-startcheck"})
        with urllib.request.urlopen(req, timeout=20, context=ctx) as response:
            data = json.loads(response.read())
        age = (data.get("broker") or {}).get("heartbeat_age_sec")
        return float(age) if age is not None else None
    except Exception:
        return None


def bridge_process_age() -> float | None:
    """Seconds since bridge_trader.py started, or None if not running."""
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*bridge_trader.py*' -and "
        f"$_.CommandLine -like '*{ROOT}*'" + " } | "
        "Sort-Object CreationDate | Select-Object -First 1 | "
        "ForEach-Object { ((Get-Date) - $_.CreationDate).TotalSeconds }"
    )
    try:
        out = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            text=True, stderr=subprocess.DEVNULL, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).strip()
        return float(out) if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def main() -> int:
    lab_url = read_env("ASIM_LAB_URL", "https://35.253.21.246")
    age = heartbeat_age(lab_url)
    proc_age = bridge_process_age()

    if age is not None and age <= FRESH_SECONDS:
        print(f"LIVE      heartbeat {age:.1f}s ago  desk={lab_url}")
        if proc_age is not None:
            print(f"          bridge_trader.py running ({proc_age / 60:.1f} min)")
        return 0

    if age is None:
        detail = "desk unreachable (network or desk down)"
    else:
        detail = f"heartbeat {age:.0f}s old (stale, limit {FRESH_SECONDS}s)"

    # Do not stampede a bridge that is mid-startup: MT5 login plus the first
    # bar fetch can take a couple of minutes before the first heartbeat lands.
    if proc_age is not None and proc_age < STARTUP_GRACE_SECONDS:
        print(f"STARTING  {detail}; bridge started {proc_age:.0f}s ago, still coming up")
        return 2

    running = "no bridge process" if proc_age is None else f"bridge up {proc_age / 60:.1f} min but not reporting"
    print(f"NOT LIVE  {detail}; {running}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
