"""Compile / presence safety gate for Ali PC + Hamza deploy.

Exit codes:
  0 = pass
  1 = fail
"""

from __future__ import annotations

import argparse
import json
import py_compile
import sys
import tempfile
from pathlib import Path

# Files that must compile when present (relative to a pack root or install root).
RUNTIME_PY = (
    "bridge_trader.py",
    "mt5_live_engine.py",
    "watchdog_exness.py",
    "healthcheck_mt5.py",
    "connection_monitor.py",
    "github_update_agent.py",
    "safety_gate_check.py",
)

# Optional under Ali PC Deployment/runtime/ or flat install root.
APP_PY = (
    "server.py",
    "broker_live.py",
    "gcp_main.py",
    "auth.py",
)

REQUIRED_STATIC = (
    "live_dashboard.html",
    "dashboard.html",
)

REQUIRED_ANY = (
    "VERSION.json",
)


def _candidates(root: Path) -> list[Path]:
    """Return search roots: install flat, repo Ali PC runtime, GCP app."""
    roots = [root]
    ali_rt = root / "Ali PC Deployment" / "runtime"
    if ali_rt.is_dir():
        roots.append(ali_rt)
    ali = root / "Ali PC Deployment"
    if ali.is_dir():
        roots.append(ali)
    gcp_app = root / "Google Console Deployment" / "app"
    if gcp_app.is_dir():
        roots.append(gcp_app)
    static = gcp_app / "static" if gcp_app.is_dir() else root / "static"
    if static.is_dir():
        roots.append(static)
    # Dedup while preserving order
    seen = set()
    out = []
    for p in roots:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def find_file(roots: list[Path], name: str) -> Path | None:
    for r in roots:
        p = r / name
        if p.is_file():
            return p
        # nested static
        p2 = r / "static" / name
        if p2.is_file():
            return p2
    return None


def compile_file(path: Path) -> str | None:
    try:
        py_compile.compile(str(path), doraise=True)
        return None
    except py_compile.PyCompileError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return f"{path}: {exc}"


def run_gate(root: Path, *, require_dashboard: bool = True) -> dict:
    roots = _candidates(root)
    errors: list[str] = []
    compiled: list[str] = []
    missing_optional: list[str] = []

    for name in RUNTIME_PY + APP_PY:
        path = find_file(roots, name)
        if path is None:
            if name in ("github_update_agent.py", "connection_monitor.py", "safety_gate_check.py"):
                # May not exist on older packs; optional until first ship.
                missing_optional.append(name)
                continue
            if name in APP_PY:
                # App files only required when GCP app tree is present.
                if any((r / name).exists() for r in roots) or any(
                    "Google Console" in str(r) for r in roots
                ):
                    # If we have a GCP app dir, require them
                    gcp = root / "Google Console Deployment" / "app"
                    if gcp.is_dir() and name in ("server.py", "broker_live.py", "gcp_main.py"):
                        errors.append(f"missing required app file: {name}")
                continue
            if name in ("bridge_trader.py", "mt5_live_engine.py", "watchdog_exness.py"):
                errors.append(f"missing required runtime file: {name}")
            continue
        err = compile_file(path)
        if err:
            errors.append(err)
        else:
            compiled.append(str(path))

    for name in REQUIRED_ANY:
        if find_file(roots, name) is None:
            # VERSION.json required under runtime or install root
            if find_file(roots, name) is None:
                # soft if only checking a tiny test fixture without VERSION
                errors.append(f"missing required file: {name}")

    if require_dashboard:
        # Only enforce when a Google Console Deployment tree or static/ exists
        has_ui_tree = (root / "Google Console Deployment" / "app").is_dir() or (
            root / "static"
        ).is_dir()
        if has_ui_tree:
            for name in REQUIRED_STATIC:
                if find_file(roots, name) is None:
                    errors.append(f"missing required dashboard file: {name}")

    # Import smoke for mt5_live_engine without MetaTrader5
    eng = find_file(roots, "mt5_live_engine.py")
    if eng is not None and not errors:
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location("_onyx_mt5_live_engine_gate", eng)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                # Avoid executing MT5 side effects: only compile already done.
                # Light check: module file is non-empty and defines tsl helpers by text.
                text = eng.read_text(encoding="utf-8", errors="replace")
                if "def tsl_distance" not in text and "effective_tsl" not in text and "TSL_PTS" not in text:
                    errors.append(f"{eng}: engine missing TSL helpers")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"engine smoke: {exc}")

    ok = len(errors) == 0
    return {
        "ok": ok,
        "root": str(root),
        "compiled": compiled,
        "missing_optional": missing_optional,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Onyxion safety / compile gate")
    parser.add_argument(
        "--root",
        default=".",
        help="Pack root, install root (C:\\onyxion-ali), or repo root",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON result",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Skip live_dashboard.html / dashboard.html presence checks",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    result = run_gate(root, require_dashboard=not args.no_dashboard)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["ok"]:
            print(f"SAFETY_GATE PASS root={root} compiled={len(result['compiled'])}")
        else:
            print(f"SAFETY_GATE FAIL root={root}")
            for e in result["errors"]:
                print(f"  - {e}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
