"""Compile / presence gate run before any code is copied to the live install
or the GCP desk is redeployed.

    python safety_gate_check.py --root <pack root | install root | repo root> [--json] [--no-dashboard]

Exit 0 = every runtime file that exists compiles, the required ones are
present, and (unless --no-dashboard) the desk pages exist when a desk tree is
present. Exit 1 = refuse to deploy.
"""

from __future__ import annotations

import argparse
import json
import py_compile
from pathlib import Path

# Must be present. Everything in this tuple is copied flat into the install root.
REQUIRED_RUNTIME = ("bridge_trader.py", "mt5_live_engine.py", "watchdog_exness.py", "VERSION.json")
# Compiled when present.
OPTIONAL_RUNTIME = ("healthcheck_mt5.py", "connection_monitor.py", "github_update_agent.py", "safety_gate_check.py")
# GCP desk app: required only when a desk tree is part of the checked root.
APP_PY = ("server.py", "broker_live.py", "gcp_main.py", "auth.py", "lab_api.py", "lab_core.py")
APP_STATIC = ("live_dashboard.html", "dashboard.html", "login.html")


def _search_roots(root: Path) -> list[Path]:
    """Install root (flat), repo layout (Ali PC Deployment/runtime), desk app (Google Console Deployment/app)."""
    roots = [root, root / "Ali PC Deployment" / "runtime", root / "Google Console Deployment" / "app",
             root / "Google Console Deployment" / "app" / "static", root / "static"]
    return [r for r in roots if r.is_dir()]


def find_file(roots: list[Path], name: str) -> Path | None:
    return next((r / name for r in roots if (r / name).is_file()), None)


def compile_file(path: Path) -> str | None:
    try:
        py_compile.compile(str(path), doraise=True)
        return None
    except py_compile.PyCompileError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return f"{path}: {exc}"


def run_gate(root: Path, *, require_dashboard: bool = True) -> dict:
    roots = _search_roots(root)
    errors: list[str] = []
    compiled: list[str] = []
    missing_optional: list[str] = []
    desk_tree = (root / "Google Console Deployment" / "app").is_dir() or (root / "static").is_dir()

    for name in REQUIRED_RUNTIME:
        path = find_file(roots, name)
        if path is None:
            errors.append(f"missing required runtime file: {name}")
        elif name.endswith(".py"):
            err = compile_file(path)
            errors.append(err) if err else compiled.append(str(path))
    for name in OPTIONAL_RUNTIME:
        path = find_file(roots, name)
        if path is None:
            missing_optional.append(name)
            continue
        err = compile_file(path)
        errors.append(err) if err else compiled.append(str(path))
    if desk_tree:
        for name in APP_PY:
            path = find_file(roots, name)
            if path is None:
                errors.append(f"missing required app file: {name}")
                continue
            err = compile_file(path)
            errors.append(err) if err else compiled.append(str(path))
        if require_dashboard:
            for name in APP_STATIC:
                if find_file(roots, name) is None:
                    errors.append(f"missing required dashboard file: {name}")

    # The bridge imports these from the engine; a rename would only surface at
    # runtime after the copy, which is exactly when it must not.
    engine = find_file(roots, "mt5_live_engine.py")
    if engine is not None:
        text = engine.read_text(encoding="utf-8", errors="replace")
        for symbol in ("class Mt5LiveEngine", "class LatestModsEngine", "def effective_hist_thresh",
                       "def tsl_distance", "def trail_live", "def effective_trail_live"):
            if symbol not in text:
                errors.append(f"{engine}: engine missing {symbol!r}")
    return {"ok": not errors, "root": str(root), "compiled": compiled,
            "missing_optional": missing_optional, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Onyxion safety / compile gate")
    parser.add_argument("--root", default=".", help="pack root, install root (C:\\onyxion-ali) or repo root")
    parser.add_argument("--json", action="store_true", help="print JSON result")
    parser.add_argument("--no-dashboard", action="store_true", help="skip dashboard presence checks")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    result = run_gate(root, require_dashboard=not args.no_dashboard)
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print(f"SAFETY_GATE PASS root={root} compiled={len(result['compiled'])}")
    else:
        print(f"SAFETY_GATE FAIL root={root}")
        for e in result["errors"]:
            print(f"  - {e}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
