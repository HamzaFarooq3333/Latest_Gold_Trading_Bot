"""Local JSON store for Asim GCP live desk."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE_DIR = Path(os.environ.get("GCP_STORE_DIR", str(HERE / "data")))
STORE_DIR.mkdir(parents=True, exist_ok=True)


def _path(model_name: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in model_name)
    return STORE_DIR / f"{safe}.json"


def default_store(model_name: str) -> dict:
    return {
        "model_name": model_name,
        "broker_live": {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def load_store(model_name: str) -> dict:
    path = _path(model_name)
    if not path.is_file():
        return default_store(model_name)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_store(model_name)


def save_store(model_name: str, store: dict) -> None:
    path = _path(model_name)
    store["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
    tmp.replace(path)
