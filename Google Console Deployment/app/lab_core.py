"""
Lab store — paper accounts, not entries. Shared by every lab; the lab is
identified by the model_name argument, so nothing here is lab-specific.

This module does not decide BUY/SELL. server.py does that. Here we:

  - persist feeds / output profiles / decision rows (S3 if LAB_STORE_BUCKET
    is set, otherwise /tmp which dies when Lambda goes cold)
  - turn a model action into CFD paper P/L (apply_paper_action)
  - pull recent gold_ohlc rows from Google Sheets for warmup
  - push decision rows to the Apps Script webhook if one is configured

XAUUSD paper math: contract 100 oz, margin = lots * 100 * price / leverage,
floating = (price - entry) * side * 100 * lots.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TMP_PATH = Path("/tmp/xauusd_lab_store.json")
_GCP_STORE_ROOT = Path(os.environ.get("GCP_STORE_DIR", "").strip() or "")
if _GCP_STORE_ROOT:
    _GCP_STORE_ROOT.mkdir(parents=True, exist_ok=True)
    TMP_PATH = _GCP_STORE_ROOT / "lab_store.json"
MAX_DECISIONS = 2500
MAX_LATENCIES = 200
MAX_ERRORS = 500
HIST_COLOR_THRESH = 10.0

# Live TradingView / Apps Script input sheet — used for model warmup so HA/regime
# state matches the real session (not the static bundled pack).
GOLD_OHLC_SHEET_ID = os.environ.get(
    "GOLD_OHLC_SHEET_ID",
    "1Yl1awz9J8mPEQdIMnelsrfYcaLLL7LrbuqjrB0WA7UA",
).strip()
GOLD_OHLC_TAB = os.environ.get("GOLD_OHLC_TAB", "gold_ohlc").strip()


def fetch_gold_ohlc_candles(n: int = 100) -> tuple[list[dict], dict]:
    """
    Load the latest N OHLC/xtrend/hist rows from the gold_ohlc Google Sheet.

    Returns (candles, meta). Candles are ordered oldest→newest for /warmup.
    Raises ValueError if the sheet cannot be read or has too few valid rows.
    """
    import csv
    import io
    import urllib.parse
    import urllib.request

    n = max(1, min(int(n or 100), 500))
    q = urllib.parse.urlencode({"tqx": "out:csv", "sheet": GOLD_OHLC_TAB})
    url = f"https://docs.google.com/spreadsheets/d/{GOLD_OHLC_SHEET_ID}/gviz/tq?{q}"
    with urllib.request.urlopen(url, timeout=45) as resp:
        text = resp.read().decode("utf-8", "replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError(f"gold_ohlc sheet empty ({GOLD_OHLC_SHEET_ID}/{GOLD_OHLC_TAB})")

    parsed: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        try:
            o = float(r.get("open"))
            h = float(r.get("high"))
            lo = float(r.get("low"))
            c = float(r.get("close"))
            xt = float(r.get("xtrend"))
            hist = float(r.get("hist"))
        except (TypeError, ValueError):
            continue
        bt = str(r.get("bar_time") or "").strip()
        # keep last occurrence of each bar_time
        if bt and bt in seen:
            parsed = [p for p in parsed if str(p.get("time") or "") != bt]
        if bt:
            seen.add(bt)
        candle = {
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "xtrend": xt,
            "hist": hist,
        }
        if bt:
            candle["time"] = bt
        parsed.append(candle)

    if len(parsed) < 14:
        raise ValueError(f"gold_ohlc has only {len(parsed)} valid OHLC rows (need >= 14)")

    candles = parsed[-n:]
    meta = {
        "source": "gold_ohlc",
        "sheet_id": GOLD_OHLC_SHEET_ID,
        "sheet_tab": GOLD_OHLC_TAB,
        "sheet_rows_valid": len(parsed),
        "fed": len(candles),
        "first_bar_time": candles[0].get("time"),
        "last_bar_time": candles[-1].get("time"),
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{GOLD_OHLC_SHEET_ID}",
    }
    return candles, meta


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def resolve_histcolor(
    body: dict | None,
    model_data: dict | None,
    hist: float,
) -> str:
    """Histogram color for analysis: green / red / orange."""
    raw = None
    if isinstance(body, dict):
        raw = body.get("histcolor") or body.get("hist_color") or body.get("histogram_color")
    if not raw and isinstance(model_data, dict):
        raw = model_data.get("color") or model_data.get("histcolor")
    if raw:
        return str(raw).strip().lower()
    h = float(hist)
    if h > HIST_COLOR_THRESH:
        return "green"
    if h < -HIST_COLOR_THRESH:
        return "red"
    return "orange"


def resolve_xtrend_touching(model_data: dict | None, candle: dict) -> bool:
    """True when the bar overlaps / touches the X-Trend line (not clear of it)."""
    if isinstance(model_data, dict):
        if model_data.get("xtrend_touching") is not None:
            return bool(model_data["xtrend_touching"])
        if model_data.get("clear_of_xtrend") is not None:
            return not bool(model_data["clear_of_xtrend"])
    xt = float(candle["xtrend"])
    buf = 0.10
    return float(candle["low"]) <= xt + buf and float(candle["high"]) >= xt - buf


def default_store(model_name: str) -> dict:
    """Empty lab: no feeds, no profiles, empty decision log."""
    mid = "".join(ch if ch.isalnum() else "_" for ch in (model_name or "MODEL").upper()).strip("_")
    slug = mid.lower()
    return {
        "version": 1,
        "model_name": model_name or mid,
        "feeds": [
            {
                "feed_id": "feed_ali",
                "feed_name": "Ali Desk Feed",
                "enabled": True,
                "linked_profile_ids": [
                    f"prof_100_{slug}",
                    f"prof_250_{slug}",
                    f"prof_500_{slug}",
                ],
                "notes": "TradingView → Apps Script sender",
            }
        ],
        "profiles": [
            {
                "profile_id": f"prof_100_{slug}",
                "profile_name": f"Paper $100 {model_name}",
                "model_name": model_name,
                "start_balance": 100.0,
                "lot_size": 0.01,
                "leverage": 100.0,
                "spread_cost": 0.30,
                "stopout_pct": 50.0,
                "enabled": True,
                "webhook_url": "",
            },
            {
                "profile_id": f"prof_250_{slug}",
                "profile_name": f"Paper $250 {model_name}",
                "model_name": model_name,
                "start_balance": 250.0,
                "lot_size": 0.01,
                "leverage": 100.0,
                "spread_cost": 0.30,
                "stopout_pct": 50.0,
                "enabled": True,
                "webhook_url": "",
            },
            {
                "profile_id": f"prof_500_{slug}",
                "profile_name": f"Paper $500 {model_name}",
                "model_name": model_name,
                "start_balance": 500.0,
                "lot_size": 0.01,
                "leverage": 100.0,
                "spread_cost": 0.30,
                "stopout_pct": 50.0,
                "enabled": True,
                "webhook_url": "",
            },
        ],
        "accounts": {},
        "decisions": [],
        "errors": [],
        # One Apps Script / Sheet webhook can receive ALL profiles (recommended).
        # Optional per-profile webhook_url overrides only if you need a different destination.
        "sheet_webhook": {
            "enabled": False,
            "url": "",
            "name": "Google Sheet receiver",
            "last_push_at": None,
            "last_push_ok": None,
            "last_push_error": None,
            "push_count": 0,
            "push_errors": 0,
        },
        "metrics": {
            "ingest_total": 0,
            "ingest_errors": 0,
            "signal_total": 0,
            "signal_errors": 0,
            "warmup_total": 0,
            "reset_total": 0,
            "decisions_total": 0,
            "actions": {},
            "latencies_ms": [],
            "last_ingest_at": None,
            "last_signal_at": None,
            "last_error": None,
            "last_error_at": None,
        },
        "updated_at": _now(),
    }


def _bucket() -> str:
    return os.environ.get("LAB_STORE_BUCKET", "").strip()


def _key(model_name: str) -> str:
    return os.environ.get(
        "LAB_STORE_KEY",
        f"lab/{model_name.lower()}/store.json",
    ).strip()


# Backtest CSV lives next to store.json, not inside it. Refreshing the dashboard
# must not lose this file; Clear Data must not delete it. Replace only.
MAX_DATASET_BYTES = 5_500_000


def dataset_keys(model_name: str) -> tuple[str, str]:
    store_key = _key(model_name)
    prefix = store_key.rsplit("/", 1)[0] if "/" in store_key else f"lab/{(model_name or 'model').lower()}"
    return f"{prefix}/backtest-dataset.csv", f"{prefix}/backtest-dataset.json"


def _dataset_tmp_paths(model_name: str) -> tuple[Path, Path]:
    slug = "".join(ch if ch.isalnum() else "_" for ch in (model_name or "MODEL").lower()).strip("_")
    if _GCP_STORE_ROOT:
        base = _GCP_STORE_ROOT
    else:
        import tempfile

        base = Path(tempfile.gettempdir())
    return base / f"{slug}_backtest-dataset.csv", base / f"{slug}_backtest-dataset.json"


def _safe_dataset_filename(name: str) -> str:
    raw = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = "".join(ch if ch.isalnum() or ch in " ._-+()[]" else "_" for ch in raw).strip(" ._")
    if not cleaned.lower().endswith(".csv"):
        cleaned = (cleaned or "dataset") + ".csv"
    return cleaned[:180] or "dataset.csv"


def _count_csv_data_rows(text: str) -> int:
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    return max(0, len(lines) - 1)


def dataset_meta(model_name: str) -> dict:
    """Filename / size of the saved backtest CSV. present=False if none yet."""
    csv_key, meta_key = dataset_keys(model_name)
    csv_tmp, meta_tmp = _dataset_tmp_paths(model_name)
    bucket = _bucket()
    meta: dict[str, Any] = {}
    if bucket:
        try:
            import boto3

            obj = boto3.client("s3").get_object(Bucket=bucket, Key=meta_key)
            meta = json.loads(obj["Body"].read().decode("utf-8"))
        except Exception as e:
            if "NoSuchKey" not in str(e) and "404" not in str(e) and "Not Found" not in str(e):
                pass
            try:
                import boto3

                head = boto3.client("s3").head_object(Bucket=bucket, Key=csv_key)
                meta = {
                    "filename": csv_key.rsplit("/", 1)[-1],
                    "bytes": int(head.get("ContentLength") or 0),
                    "rows": None,
                    "uploaded_at": str(head.get("LastModified") or ""),
                }
            except Exception:
                meta = {}
    elif meta_tmp.is_file():
        try:
            meta = json.loads(meta_tmp.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    if not meta and csv_tmp.is_file():
        meta = {
            "filename": csv_tmp.name,
            "bytes": csv_tmp.stat().st_size,
            "rows": None,
            "uploaded_at": "",
        }
    if not meta:
        return {"present": False}
    return {
        "present": True,
        "filename": meta.get("filename") or "dataset.csv",
        "bytes": int(meta.get("bytes") or 0),
        "rows": meta.get("rows"),
        "uploaded_at": meta.get("uploaded_at") or "",
    }


def load_dataset_csv(model_name: str) -> tuple[str | None, dict]:
    """Return (csv_text, meta). csv_text is None when nothing is saved."""
    csv_key, _meta_key = dataset_keys(model_name)
    csv_tmp, _meta_tmp = _dataset_tmp_paths(model_name)
    bucket = _bucket()
    text = None
    if bucket:
        try:
            import boto3

            obj = boto3.client("s3").get_object(Bucket=bucket, Key=csv_key)
            text = obj["Body"].read().decode("utf-8", "replace")
        except Exception as e:
            if "NoSuchKey" not in str(e) and "404" not in str(e) and "Not Found" not in str(e):
                pass
    if text is None and csv_tmp.is_file():
        text = csv_tmp.read_text(encoding="utf-8", errors="replace")
    meta = dataset_meta(model_name)
    if text is None:
        return None, {"present": False}
    meta["present"] = True
    meta["bytes"] = len(text.encode("utf-8"))
    if meta.get("rows") is None:
        meta["rows"] = _count_csv_data_rows(text)
    return text, meta


def save_dataset_csv(model_name: str, csv_text: str, filename: str = "dataset.csv") -> dict:
    """Replace the saved backtest CSV. The previous file is overwritten."""
    text = str(csv_text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = text.encode("utf-8")
    if len(raw) < 40:
        raise ValueError("CSV is empty")
    if len(raw) > MAX_DATASET_BYTES:
        raise ValueError(f"CSV is too large (max {MAX_DATASET_BYTES} bytes)")
    header = (text.split("\n", 1)[0] or "").lower()
    if "open" not in header or "close" not in header:
        raise ValueError("CSV needs open and close columns")
    rows = _count_csv_data_rows(text)
    if rows < 10:
        raise ValueError("CSV needs at least 10 data rows")
    fname = _safe_dataset_filename(filename)
    meta = {
        "filename": fname,
        "bytes": len(raw),
        "rows": rows,
        "uploaded_at": _now(),
    }
    csv_key, meta_key = dataset_keys(model_name)
    csv_tmp, meta_tmp = _dataset_tmp_paths(model_name)
    csv_tmp.write_bytes(raw)
    meta_tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    bucket = _bucket()
    if bucket:
        import boto3

        s3 = boto3.client("s3")
        s3.put_object(Bucket=bucket, Key=csv_key, Body=raw, ContentType="text/csv; charset=utf-8")
        s3.put_object(
            Bucket=bucket, Key=meta_key,
            Body=json.dumps(meta, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    return {"present": True, **meta}


def save_store(model_name: str, store: dict) -> None:
    """
    Write lab json to /tmp always, and to S3 when LAB_STORE_BUCKET is set.

    Concurrent Lambda invocations (a webhook retry landing while the dashboard
    writes) used to clobber each other last-writer-wins, silently dropping
    decisions and paper-account updates. The S3 write is now a compare-and-swap
    against the ETag the store was read with: on a conflict we re-read, replay
    this invocation's appends onto the newer store, and retry.
    """
    store["updated_at"] = _now()
    bucket = _bucket()
    if not bucket:
        TMP_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")
        return

    import boto3

    s3 = boto3.client("s3")
    key = _key(model_name)
    expected = store.pop("_etag", None)

    for attempt in range(_SAVE_MAX_ATTEMPTS):
        raw = json.dumps(store, indent=2)
        kwargs = {
            "Bucket": bucket,
            "Key": key,
            "Body": raw.encode("utf-8"),
            "ContentType": "application/json",
        }
        # If-Match makes the PUT fail rather than overwrite a newer version.
        # IfNoneMatch="*" is the first-write case (object must not exist yet).
        if expected:
            kwargs["IfMatch"] = expected
        try:
            resp = s3.put_object(**kwargs)
            store["_etag"] = (resp.get("ETag") or "").strip('"') or None
            TMP_PATH.write_text(raw, encoding="utf-8")
            return
        except Exception as e:
            msg = str(e)
            conflict = (
                "PreconditionFailed" in msg
                or "ConditionalRequestConflict" in msg
                or "412" in msg
                or "409" in msg
            )
            # A bucket or SDK without conditional-write support must still save.
            unsupported = "NotImplemented" in msg or "InvalidArgument" in msg
            if unsupported:
                s3.put_object(
                    Bucket=bucket, Key=key,
                    Body=raw.encode("utf-8"), ContentType="application/json",
                )
                TMP_PATH.write_text(raw, encoding="utf-8")
                return
            if not conflict or attempt == _SAVE_MAX_ATTEMPTS - 1:
                # Last resort: never lose the write entirely.
                s3.put_object(
                    Bucket=bucket, Key=key,
                    Body=raw.encode("utf-8"), ContentType="application/json",
                )
                TMP_PATH.write_text(raw, encoding="utf-8")
                return
            # Someone else wrote first. Rebase our appends onto their store.
            fresh, expected = _read_store_with_etag(model_name)
            if fresh is None:
                expected = None
                continue
            store = _merge_store(fresh, store)
            store["updated_at"] = _now()


_SAVE_MAX_ATTEMPTS = 4


def _read_store_with_etag(model_name: str):
    """Return (store, etag) from S3, or (None, None) when unreadable."""
    bucket = _bucket()
    if not bucket:
        return None, None
    try:
        import boto3

        obj = boto3.client("s3").get_object(Bucket=bucket, Key=_key(model_name))
        raw = obj["Body"].read().decode("utf-8")
        etag = (obj.get("ETag") or "").strip('"') or None
        return ensure_store_shape(json.loads(raw), model_name), etag
    except Exception:
        return None, None


_APPEND_LISTS = ("decisions", "errors")


def _merge_store(base: dict, mine: dict) -> dict:
    """
    Rebase this invocation's changes onto a store someone else just wrote.

    Append-only logs are unioned by id so neither writer's rows are lost.
    Config and account state take this invocation's value, because a paper
    account is only ever advanced by the ingest that is holding it.
    """
    merged = dict(base)
    for name in _APPEND_LISTS:
        theirs = list(base.get(name) or [])
        ours = list(mine.get(name) or [])
        seen = set()
        out = []
        for row in theirs + ours:
            rid = row.get("id") or row.get("decision_id") or row.get("error_id")
            marker = rid if rid is not None else json.dumps(row, sort_keys=True, default=str)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(row)
        merged[name] = out[-MAX_DECISIONS:] if name == "decisions" else out[-MAX_ERRORS:]
    for name in ("feeds", "profiles", "accounts", "sheet_webhook", "metrics"):
        if name in mine:
            merged[name] = mine[name]
    # Heartbeats are frequent and can race with an ingest/config write. Keep
    # the newest broker snapshot instead of allowing a stale rebase to erase it.
    base_broker = base.get("broker_live")
    mine_broker = mine.get("broker_live")
    if base_broker is not None or mine_broker is not None:
        base_stamp = str(
            (base_broker or {}).get("updated_at")
            or (base_broker or {}).get("last_heartbeat_at")
            or ""
        )
        mine_stamp = str(
            (mine_broker or {}).get("updated_at")
            or (mine_broker or {}).get("last_heartbeat_at")
            or ""
        )
        merged["broker_live"] = mine_broker if mine_stamp >= base_stamp else base_broker
    return merged


def ensure_account(store: dict, profile: dict) -> dict:
    """Get or create the paper CFD account hanging off this output profile."""
    pid = profile["profile_id"]
    acc = store["accounts"].get(pid)
    if not acc:
        start = float(profile["start_balance"])
        acc = {
            "banked_balance": start,
            "units": [],
            "side": "FLAT",
            "peak_equity": start,
            "max_drawdown_pct": 0.0,
            "trades": 0,
            "skips": 0,
            "stopouts": 0,
        }
        store["accounts"][pid] = acc
    return acc


# XAUUSD CFD contract size (1.00 lot = 100 troy ounces) — industry standard on MT4/MT5.
XAU_CONTRACT_SIZE = 100.0


# A unit is stored as {"entry": px, "lot": lots}. Older stores held a bare float
# entry price and priced every unit at the profile lot, which mis-booked every
# fill the engine doubled in a best hour. Both shapes are accepted on read.
def _unit_entry(u) -> float:
    if isinstance(u, dict):
        return float(u.get("entry"))
    return float(u)


def _unit_lot(u, default_lot: float) -> float:
    if isinstance(u, dict) and u.get("lot") is not None:
        return float(u["lot"])
    return float(default_lot)


def _make_unit(entry: float, lot: float) -> dict:
    return {"entry": float(entry), "lot": float(lot)}


def _floating(acc: dict, price: float, lot: float, contract: float = XAU_CONTRACT_SIZE) -> float:
    """Unrealized PnL = sum((mark - entry) * direction * contract * lots) per open unit."""
    side = acc.get("side")
    units = acc.get("units") or []
    if side == "LONG":
        return sum((price - _unit_entry(u)) * contract * _unit_lot(u, lot) for u in units)
    if side == "SHORT":
        return sum((_unit_entry(u) - price) * contract * _unit_lot(u, lot) for u in units)
    return 0.0


def _margin(acc: dict, price: float, lot: float, leverage: float, contract: float = XAU_CONTRACT_SIZE) -> float:
    """Used margin = Σ(unit lots) * contract * price / leverage."""
    units = acc.get("units") or []
    if not units or leverage <= 0:
        return 0.0
    return sum(_unit_lot(u, lot) for u in units) * contract * price / leverage


def apply_paper_action(
    store: dict,
    profile: dict,
    signal: str,
    price: float,
    entry_hint: float | None = None,
    lot_override: float | None = None,
) -> dict:
    """
    Paper account update from a model signal.

    Standard CFD / MT formulas (USD account, XAUUSD):
      notional      = lots * contract_size * price
      required_margin = notional / leverage
      floating_pnl  = Σ (price − entry) * ±1 * contract * lots   (+ LONG, − SHORT)
      equity        = balance + floating_pnl
      free_margin   = equity − used_margin
      margin_level% = (equity / used_margin) * 100   (None if flat)
      stop-out      when margin_level% < stopout_pct
      max_drawdown% = max over time of (peak_equity − equity) / peak_equity * 100

    spread_cost is a flat USD fee deducted from balance on each successful fill
    (simplified vs bid/ask mid-point; keeps paper sim deterministic).
    """
    acc = ensure_account(store, profile)
    lot = float(profile["lot_size"])
    # The engine doubles its lot in "best" hours (BEST_LOT_MULT). Book the size
    # it actually filled, not the profile default, or every best-hour trade is
    # recorded at half its real P/L, floating and margin.
    fill_lot = float(lot_override) if lot_override else lot
    lev = float(profile["leverage"])
    spread = float(profile.get("spread_cost", 0.3))
    stopout = float(profile.get("stopout_pct", 50.0))
    contract = XAU_CONTRACT_SIZE

    action = (signal or "NONE").upper()
    status_note = ""
    realized = None

    # stop-out check before new risk
    used = _margin(acc, price, lot, lev, contract)
    eq = acc["banked_balance"] + _floating(acc, price, lot, contract)
    if used > 0 and (eq / used) * 100 < stopout and acc["units"]:
        realized = _floating(acc, price, lot, contract)
        acc["banked_balance"] += realized
        acc["units"] = []
        acc["side"] = "FLAT"
        acc["stopouts"] += 1
        action = "STOP_OUT"
        status_note = "LIQUIDATION" + (" /WIPED" if acc["banked_balance"] <= 1 else "")
    else:
        want = None
        if action in ("BUY", "BUY_ADD"):
            want = "LONG"
        elif action in ("SELL", "SELL_ADD"):
            want = "SHORT"
        elif action == "EXIT":
            if acc["units"]:
                realized = _floating(acc, price, lot, contract)
                acc["banked_balance"] += realized
                acc["units"] = []
                acc["side"] = "FLAT"
                status_note = "exit color-flip"
            else:
                status_note = "exit flat"
        elif action in ("EXIT_ONE", "EXIT_PRIMARY"):
            if acc["units"]:
                if action == "EXIT_PRIMARY":
                    idx = 0
                elif entry_hint is not None:
                    idx = min(
                        range(len(acc["units"])),
                        key=lambda i: abs(_unit_entry(acc["units"][i]) - float(entry_hint)),
                    )
                else:
                    idx = len(acc["units"]) - 1
                unit = acc["units"].pop(idx)
                entry = _unit_entry(unit)
                unit_lot = _unit_lot(unit, lot)
                realized = (
                    (price - entry) if acc["side"] == "LONG" else (entry - price)
                ) * contract * unit_lot
                acc["banked_balance"] += realized
                if not acc["units"]:
                    acc["side"] = "FLAT"
                status_note = "exit primary stop" if action == "EXIT_PRIMARY" else "exit supplementary stop"
            else:
                status_note = "exit unit while flat"
        elif action in ("HOLD", "NONE"):
            status_note = action.lower()

        if want:
            # flip
            if acc["side"] in ("LONG", "SHORT") and acc["side"] != want and acc["units"]:
                realized = _floating(acc, price, lot, contract)
                acc["banked_balance"] += realized
                acc["units"] = []
                acc["side"] = "FLAT"
                status_note = "flip-close;"

            # margin gate for new unit
            free = (acc["banked_balance"] + _floating(acc, price, lot, contract)) - _margin(
                acc, price, lot, lev, contract
            )
            need = fill_lot * contract * price / lev
            if free >= need + spread:
                acc["banked_balance"] -= spread  # spread cost on fill
                acc["units"].append(_make_unit(price, fill_lot))
                acc["side"] = want
                acc["trades"] += 1
                status_note += "executed"
            else:
                acc["skips"] += 1
                status_note += "SKIP-no-margin"
                if action.endswith("_ADD"):
                    pass
                else:
                    action = action  # keep requested action label

    flot = _floating(acc, price, lot, contract)
    used = _margin(acc, price, lot, lev, contract)
    equity = acc["banked_balance"] + flot
    free = equity - used
    mlevel = (equity / used * 100) if used > 0 else None
    if equity > acc["peak_equity"]:
        acc["peak_equity"] = equity
    dd = 0.0
    if acc["peak_equity"] > 0:
        dd = max(0.0, (acc["peak_equity"] - equity) / acc["peak_equity"] * 100)
        acc["max_drawdown_pct"] = max(float(acc.get("max_drawdown_pct") or 0), dd)

    return {
        "signal": action,
        "position_side": acc["side"],
        "open_units": len(acc["units"]),
        "start_balance": float(profile["start_balance"]),
        "banked_balance": round(acc["banked_balance"], 4),
        "floating_pnl": round(flot, 4),
        "equity": round(equity, 4),
        "margin_used": round(used, 4),
        "free_margin": round(free, 4),
        "margin_level_pct": None if mlevel is None else round(mlevel, 2),
        "realized_pnl": None if realized is None else round(realized, 4),
        "open_lots": round(sum(_unit_lot(u, lot) for u in acc["units"]), 4),
        "fill_lot": fill_lot,
        "lot_size": lot,
        "leverage": lev,
        "spread_cost": spread,
        "stopout_pct": stopout,
        "status_note": status_note or action,
        "peak_equity": round(acc["peak_equity"], 4),
        "max_drawdown_pct": round(float(acc.get("max_drawdown_pct") or 0), 3),
        "account_trades": acc["trades"],
        "account_skips": acc["skips"],
        "account_stopouts": acc["stopouts"],
    }


def record_metric(
    store: dict,
    kind: str,
    ms: float | None = None,
    action: str | None = None,
    error: str | None = None,
    where: str | None = None,
    detail: str | None = None,
    feed_id: str | None = None,
    profile_id: str | None = None,
    http_status: int | None = None,
):
    m = store["metrics"]
    if kind == "ingest":
        m["ingest_total"] = int(m.get("ingest_total") or 0) + 1
        m["last_ingest_at"] = _now()
    elif kind == "signal":
        m["signal_total"] = int(m.get("signal_total") or 0) + 1
        m["last_signal_at"] = _now()
    elif kind == "warmup":
        m["warmup_total"] = int(m.get("warmup_total") or 0) + 1
    elif kind == "reset":
        m["reset_total"] = int(m.get("reset_total") or 0) + 1
    elif kind == "ingest_error":
        m["ingest_errors"] = int(m.get("ingest_errors") or 0) + 1
    elif kind == "signal_error":
        m["signal_errors"] = int(m.get("signal_errors") or 0) + 1

    if ms is not None:
        lat = list(m.get("latencies_ms") or [])
        lat.append(round(float(ms), 2))
        m["latencies_ms"] = lat[-MAX_LATENCIES:]
    if action:
        acts = dict(m.get("actions") or {})
        acts[action] = int(acts.get(action) or 0) + 1
        m["actions"] = acts
    if error:
        m["last_error"] = str(error)[:400]
        m["last_error_at"] = _now()
        append_error(
            store,
            where=where or kind,
            reason=str(error),
            detail=detail,
            feed_id=feed_id,
            profile_id=profile_id,
            http_status=http_status,
            latency_ms=ms,
        )


def append_error(
    store: dict,
    *,
    where: str,
    reason: str,
    detail: str | None = None,
    feed_id: str | None = None,
    profile_id: str | None = None,
    http_status: int | None = None,
    latency_ms: float | None = None,
) -> dict:
    """Append a structured error for the Errors log (time · where · reason)."""
    row = {
        "error_id": new_id("err"),
        "at": _now(),
        "where": str(where or "unknown")[:80],
        "reason": str(reason or "")[:500],
        "detail": str(detail or "")[:800],
        "feed_id": feed_id or "",
        "profile_id": profile_id or "",
        "http_status": http_status,
        "latency_ms": None if latency_ms is None else round(float(latency_ms), 2),
    }
    errs = list(store.get("errors") or [])
    errs.append(row)
    store["errors"] = errs[-MAX_ERRORS:]
    return row


def append_decision(store: dict, row: dict):
    m = store["metrics"]
    m["decisions_total"] = int(m.get("decisions_total") or 0) + 1
    dec = list(store.get("decisions") or [])
    dec.append(row)
    store["decisions"] = dec[-MAX_DECISIONS:]


def _num_or_none(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _boolish(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in ("1", "true", "yes", "y", "t")


def import_sheet_decisions(store: dict, rows: list[dict], *, replace: bool = True) -> dict:
    """
    Import decision rows from Google Sheet / CSV export into the lab store.
    Rebuilds per-profile paper accounts from the latest row for each profile.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows must be a non-empty list")

    normalized: list[dict] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("profile_id") or "").strip()
        if not pid:
            continue
        note = str(raw.get("status_note") or "")
        row = {
            "received_at": str(raw.get("received_at") or raw.get("pushed_at") or _now()),
            "feed_id": str(raw.get("feed_id") or ""),
            "feed_name": str(raw.get("feed_name") or ""),
            "profile_id": pid,
            "profile_name": str(raw.get("profile_name") or ""),
            "model_name": str(raw.get("model_name") or store.get("model_name") or ""),
            "bar_time": str(raw.get("bar_time") or ""),
            "source": str(raw.get("source") or "webhook"),
            "signal": str(raw.get("signal") or "NONE"),
            "model_raw_action": str(raw.get("model_raw_action") or raw.get("signal") or "NONE"),
            "histcolor": str(raw.get("histcolor") or ""),
            "xtrend_touching": _boolish(raw.get("xtrend_touching")),
            "open": _num_or_none(raw.get("open")),
            "high": _num_or_none(raw.get("high")),
            "low": _num_or_none(raw.get("low")),
            "close": _num_or_none(raw.get("close")),
            "mark_price": _num_or_none(raw.get("mark_price") if raw.get("mark_price") not in (None, "") else raw.get("close")),
            "xtrend": _num_or_none(raw.get("xtrend")),
            "hist": _num_or_none(raw.get("hist")),
            "model_latency_ms": _num_or_none(raw.get("model_latency_ms")),
            "position_side": str(raw.get("position_side") or "FLAT"),
            "open_units": int(float(raw.get("open_units") or 0)),
            "start_balance": _num_or_none(raw.get("start_balance")),
            "banked_balance": _num_or_none(raw.get("banked_balance")),
            "floating_pnl": _num_or_none(raw.get("floating_pnl")) or 0.0,
            "equity": _num_or_none(raw.get("equity")),
            "margin_used": _num_or_none(raw.get("margin_used")) or 0.0,
            "free_margin": _num_or_none(raw.get("free_margin")),
            "margin_level_pct": _num_or_none(raw.get("margin_level_pct")),
            "realized_pnl": _num_or_none(raw.get("realized_pnl")),
            "lot_size": _num_or_none(raw.get("lot_size")),
            "fill_lot": _num_or_none(raw.get("fill_lot")),
            "leverage": _num_or_none(raw.get("leverage")),
            "spread_cost": _num_or_none(raw.get("spread_cost")),
            "stopout_pct": _num_or_none(raw.get("stopout_pct")),
            "status_note": note,
            "peak_equity": _num_or_none(raw.get("peak_equity")),
            "max_drawdown_pct": _num_or_none(raw.get("max_drawdown_pct")) or 0.0,
            "account_trades": int(float(raw.get("account_trades") or 0)),
            "account_skips": int(float(raw.get("account_skips") or 0)),
            "account_stopouts": int(float(raw.get("account_stopouts") or 0)),
        }
        if row["equity"] is None and row["banked_balance"] is not None:
            row["equity"] = float(row["banked_balance"]) + float(row["floating_pnl"] or 0)
        normalized.append(row)

    if not normalized:
        raise ValueError("no valid rows (need profile_id)")

    def _sort_key(r: dict):
        return (str(r.get("bar_time") or ""), str(r.get("received_at") or ""))

    normalized.sort(key=_sort_key)

    if replace:
        store["decisions"] = normalized[-MAX_DECISIONS:]
    else:
        existing = list(store.get("decisions") or [])
        existing.extend(normalized)
        store["decisions"] = existing[-MAX_DECISIONS:]

    # Rebuild paper accounts from latest row per profile
    latest: dict[str, dict] = {}
    counts: dict[str, dict] = {}
    for r in store["decisions"]:
        pid = r["profile_id"]
        latest[pid] = r
        c = counts.setdefault(pid, {"trades": 0, "skips": 0, "stopouts": 0})
        note = str(r.get("status_note") or "").lower()
        if "executed" in note:
            c["trades"] += 1
        if "skip" in note:
            c["skips"] += 1
        if "liquid" in note or "stop_out" in note or str(r.get("signal") or "").upper() == "STOP_OUT":
            c["stopouts"] += 1

    accounts = dict(store.get("accounts") or {})
    for pid, r in latest.items():
        start = float(r.get("start_balance") or 0)
        banked = float(r.get("banked_balance") if r.get("banked_balance") is not None else start)
        side = str(r.get("position_side") or "FLAT")
        n_units = int(r.get("open_units") or 0)
        mark = float(r.get("mark_price") or r.get("close") or 0)
        # Sheet export does not include entry prices; approximate open units at mark.
        rebuilt_lot = float(r.get("fill_lot") or r.get("lot_size") or 0.01)
        units = (
            [_make_unit(mark, rebuilt_lot) for _ in range(n_units)]
            if side in ("LONG", "SHORT") and n_units > 0 and mark > 0
            else []
        )
        c = counts.get(pid) or {}
        peak = float(r.get("peak_equity") or max(start, float(r.get("equity") or banked)))
        accounts[pid] = {
            "banked_balance": banked,
            "units": units,
            "side": side if units else "FLAT",
            "peak_equity": peak,
            "max_drawdown_pct": float(r.get("max_drawdown_pct") or 0),
            "trades": int(c.get("trades") or 0),
            "skips": int(c.get("skips") or 0),
            "stopouts": int(c.get("stopouts") or 0),
        }
    store["accounts"] = accounts

    m = store.setdefault("metrics", {})
    m["decisions_total"] = len(store["decisions"])
    bars = sorted({str(r.get("bar_time") or "") for r in store["decisions"] if r.get("bar_time")})
    if bars:
        m["ingest_total"] = len(bars)
        m["signal_total"] = len(bars)
    actions: dict[str, int] = {}
    for r in store["decisions"]:
        sig = str(r.get("signal") or "NONE").upper()
        actions[sig] = int(actions.get(sig, 0)) + 1
    m["actions"] = actions
    # Use newest received_at so the dashboard timer treats this as fresh data arrival.
    newest = max((str(r.get("received_at") or "") for r in store["decisions"]), default="")
    if newest:
        m["last_ingest_at"] = newest
        m["last_signal_at"] = newest

    return {
        "imported": len(normalized),
        "decisions": len(store["decisions"]),
        "profiles_updated": len(latest),
        "replace": bool(replace),
        "last_ingest_at": m.get("last_ingest_at"),
    }


def metrics_summary(store: dict) -> dict:
    m = store.get("metrics") or {}
    lat = list(m.get("latencies_ms") or [])
    summary = {
        "ingest_total": m.get("ingest_total", 0),
        "ingest_errors": m.get("ingest_errors", 0),
        "signal_total": m.get("signal_total", 0),
        "signal_errors": m.get("signal_errors", 0),
        "warmup_total": m.get("warmup_total", 0),
        "reset_total": m.get("reset_total", 0),
        "decisions_total": m.get("decisions_total", 0),
        "actions": m.get("actions") or {},
        "last_ingest_at": m.get("last_ingest_at"),
        "last_signal_at": m.get("last_signal_at"),
        "last_error": m.get("last_error"),
        "last_error_at": m.get("last_error_at"),
        "errors_total": len(store.get("errors") or []),
        "latency": {},
    }
    if lat:
        s = sorted(lat)
        summary["latency"] = {
            "n": len(s),
            "avg_ms": round(sum(s) / len(s), 2),
            "p50_ms": s[len(s) // 2],
            "p95_ms": s[min(len(s) - 1, int(len(s) * 0.95))],
            "max_ms": s[-1],
            "min_ms": s[0],
        }
    # account rollup — all profiles (even unused)
    accounts = []
    for p in store.get("profiles") or []:
        acc = store.get("accounts", {}).get(p["profile_id"]) or {}
        start = float(p.get("start_balance") or 0)
        lot = float(p.get("lot_size") or 0.01)
        lev = float(p.get("leverage") or 500)
        # mark-to-market using last decision close if available, else entry / start
        last_px = None
        for d in reversed(store.get("decisions") or []):
            if d.get("profile_id") == p["profile_id"] and d.get("mark_price") is not None:
                last_px = float(d["mark_price"])
                break
            if d.get("profile_id") == p["profile_id"] and d.get("close") is not None:
                last_px = float(d["close"])
                break
        units = acc.get("units") or []
        if last_px is None and units:
            last_px = _unit_entry(units[-1])
        if last_px is None:
            last_px = 0.0
        flot = _floating(acc, last_px, lot) if units else 0.0
        used = _margin(acc, last_px, lot, lev) if units else 0.0
        banked = float(acc.get("banked_balance", start))
        equity = banked + flot
        accounts.append(
            {
                "profile_id": p["profile_id"],
                "profile_name": p["profile_name"],
                "banked_balance": round(banked, 2),
                "floating_pnl": round(flot, 2),
                "equity": round(equity, 2),
                "margin_used": round(used, 2),
                "free_margin": round(equity - used, 2),
                "margin_level_pct": None if used <= 0 else round(equity / used * 100, 2),
                "open_units": len(units),
                "position_side": acc.get("side") or "FLAT",
                "peak_equity": round(float(acc.get("peak_equity", start)), 2),
                "max_drawdown_pct": round(float(acc.get("max_drawdown_pct") or 0), 2),
                "trades": acc.get("trades", 0),
                "skips": acc.get("skips", 0),
                "stopouts": acc.get("stopouts", 0),
                "start_balance": start,
                "lot_size": p.get("lot_size"),
                "leverage": p.get("leverage"),
                "enabled": p.get("enabled", True),
                "mark_price": last_px or None,
            }
        )
    summary["accounts"] = accounts
    summary["feeds_count"] = len(store.get("feeds") or [])
    summary["profiles_count"] = len(store.get("profiles") or [])
    summary["store_updated_at"] = store.get("updated_at")
    summary["persistence"] = "s3" if _bucket() else "tmp"
    return summary


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# PIN the dashboard asks for before wiping paper logs. Not a trading API key.
# Leave it if you send this folder internally; do not post the zip publicly.
CLEAR_PASSWORD = "333"


def seed_demo_trades(store: dict, model_name: str, n_bars: int = 24) -> dict:
    """
    Wipe paper records and replay a deterministic sample trade path through
    apply_paper_action so Home / Analysis / CSV show realistic CFD numbers.
    """
    import random

    clear_lab_data(store, model_name, "records")
    rng = random.Random(42)
    mid = (model_name or store.get("model_name") or "HARD").upper()
    feed = (store.get("feeds") or [{}])[0]
    feed_id = feed.get("feed_id") or "feed_ali"
    feed_name = feed.get("feed_name") or "Ali Desk Feed"
    profiles = [p for p in (store.get("profiles") or []) if p.get("enabled") is not False]
    if not profiles:
        raise ValueError("no profiles to seed")

    # Shared price path around XAU ~4340
    price = 4335.0
    path = []
    for i in range(n_bars):
        price = round(price + rng.uniform(-4.5, 5.5), 2)
        path.append(price)

    # Per-profile signal scripts (cycled / truncated to n_bars)
    scripts = {
        0: [  # $250 — active long then flip short, leave open short
            "BUY", "HOLD", "HOLD", "BUY_ADD", "HOLD", "HOLD", "SELL", "HOLD",
            "HOLD", "SELL_ADD", "HOLD", "HOLD", "HOLD", "BUY", "HOLD", "HOLD",
            "EXIT", "HOLD", "BUY", "HOLD", "HOLD", "SELL", "HOLD", "HOLD",
        ],
        1: [  # $1000 — smoother winner, ends LONG
            "HOLD", "BUY", "HOLD", "HOLD", "HOLD", "BUY_ADD", "HOLD", "HOLD",
            "HOLD", "HOLD", "EXIT", "HOLD", "BUY", "HOLD", "HOLD", "HOLD",
            "BUY_ADD", "HOLD", "HOLD", "HOLD", "HOLD", "HOLD", "HOLD", "HOLD",
        ],
        2: [  # $5000 — more trades / skips flavor
            "BUY", "HOLD", "SELL", "HOLD", "BUY", "HOLD", "SELL", "HOLD",
            "BUY", "BUY_ADD", "HOLD", "EXIT", "SELL", "HOLD", "HOLD", "SELL_ADD",
            "HOLD", "BUY", "HOLD", "EXIT", "HOLD", "BUY", "HOLD", "HOLD",
        ],
    }

    base_ts = datetime(2026, 8, 8, 10, 0, 0, tzinfo=timezone.utc)
    rows_made = 0
    for pi, profile in enumerate(profiles):
        script = scripts.get(pi % 3, scripts[0])
        for i, px in enumerate(path):
            sig = script[i % len(script)]
            snap = apply_paper_action(store, profile, sig, px)
            bar_time = (base_ts.timestamp() + i * 900)
            bt = datetime.fromtimestamp(bar_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            o = round(px - rng.uniform(0.2, 1.5), 2)
            hi = round(px + rng.uniform(0.5, 2.0), 2)
            lo = round(px - rng.uniform(0.5, 2.0), 2)
            xt = round(px - rng.uniform(-3, 3), 2)
            hv = round(rng.uniform(-15, 20), 2)
            row = {
                "received_at": _now(),
                "feed_id": feed_id,
                "feed_name": feed_name,
                "profile_id": profile["profile_id"],
                "profile_name": profile.get("profile_name"),
                "model_name": mid,
                "bar_time": bt,
                "source": "seed",
                "model_raw_action": sig,
                "histcolor": resolve_histcolor(None, None, hv),
                "xtrend_touching": bool(lo <= xt <= hi),
                "close": px,
                "mark_price": px,
                "open": o,
                "high": hi,
                "low": lo,
                "xtrend": xt,
                "hist": hv,
                "model_latency_ms": round(rng.uniform(40, 180), 1),
                **snap,
            }
            append_decision(store, row)
            record_metric(store, "ingest", ms=row["model_latency_ms"], action=snap["signal"])
            rows_made += 1

    m = store.setdefault("metrics", {})
    m["signal_total"] = int(m.get("signal_total") or 0) + n_bars
    m["last_signal_at"] = _now()
    m["last_ingest_at"] = _now()

    # Sample error log entries so Errors UI is visible immediately
    samples = [
        {
            "where": "ingest",
            "reason": "unknown feed_id feed_missing_demo",
            "detail": "Ingest rejected because feed_id was not registered",
            "feed_id": "feed_missing_demo",
            "http_status": 404,
        },
        {
            "where": "signal",
            "reason": "model upstream timeout",
            "detail": "EC2 /signal did not respond within 60s during demo seed",
            "http_status": 504,
            "latency_ms": 60000,
        },
        {
            "where": "webhook",
            "reason": "sheet push failed",
            "detail": "HTTP 500 from Apps Script receiver (demo sample)",
            "http_status": 500,
            "profile_id": profiles[0]["profile_id"] if profiles else "",
        },
        {
            "where": "paper",
            "reason": "SKIP-no-margin (demo)",
            "detail": "Illustrative skip when free margin < required margin for new unit",
            "profile_id": profiles[0]["profile_id"] if profiles else "",
            "feed_id": feed_id,
        },
    ]
    for s in samples:
        append_error(store, **s)
        m["ingest_errors"] = int(m.get("ingest_errors") or 0) + (1 if s["where"] == "ingest" else 0)
        m["signal_errors"] = int(m.get("signal_errors") or 0) + (1 if s["where"] == "signal" else 0)
        m["last_error"] = s["reason"]
        m["last_error_at"] = _now()

    return {
        "rows": rows_made,
        "bars": n_bars,
        "profiles": len(profiles),
        "errors": len(store.get("errors") or []),
        "accounts": metrics_summary(store).get("accounts"),
    }


def clear_lab_data(store: dict, model_name: str, scope: str) -> dict:
    """Wipe decisions / everything. Called after the dashboard PIN check."""
    """
    scope:
      decisions — trade/decision CSV rows + paper accounts only (keep webhooks/feeds)
      records   — decisions/CSV + paper accounts + counters + errors
      webhooks  — shared + per-profile webhook settings
      everything — records + webhooks + reset feeds/profiles to defaults
    """
    scope = (scope or "").strip().lower()
    if scope not in ("decisions", "records", "webhooks", "everything"):
        raise ValueError("scope must be decisions|records|webhooks|everything")

    if scope == "decisions":
        store["decisions"] = []
        store["accounts"] = {}
        m = store.setdefault("metrics", {})
        m["decisions_total"] = 0
        m["actions"] = {}
        # Ensure the dashboard timer and "first/last trade" views reset.
        m["last_ingest_at"] = None
        m["last_signal_at"] = None
        m["last_error"] = None
        m["last_error_at"] = None
        return store

    if scope in ("records", "everything"):
        store["decisions"] = []
        store["accounts"] = {}
        store["errors"] = []
        m = store.setdefault("metrics", {})
        for k in (
            "ingest_total",
            "ingest_errors",
            "signal_total",
            "signal_errors",
            "warmup_total",
            "reset_total",
            "decisions_total",
        ):
            m[k] = 0
        m["actions"] = {}
        m["latencies_ms"] = []
        m["last_ingest_at"] = None
        m["last_signal_at"] = None
        m["last_error"] = None
        m["last_error_at"] = None

    if scope in ("webhooks", "everything"):
        store["sheet_webhook"] = {
            "enabled": False,
            "url": "",
            "name": "Google Sheet receiver",
            "last_push_at": None,
            "last_push_ok": None,
            "last_push_error": None,
            "push_count": 0,
            "push_errors": 0,
        }
        for p in store.get("profiles") or []:
            p["webhook_url"] = ""

    if scope == "everything":
        # Full wipe: no pipelines, no outputs, no prior trade state.
        # (Do NOT repopulate defaults; user expects the lab to look like it never existed.)
        store["feeds"] = []
        store["profiles"] = []
        store["accounts"] = {}
        store["decisions"] = []

        # Ensure metrics are zeroed even if we already cleared records/webhooks above.
        m = store.setdefault("metrics", {})
        for k in (
            "ingest_total",
            "ingest_errors",
            "signal_total",
            "signal_errors",
            "warmup_total",
            "reset_total",
            "decisions_total",
        ):
            m[k] = 0
        m["actions"] = {}
        m["latencies_ms"] = []
        m["last_ingest_at"] = None
        m["last_signal_at"] = None
        m["last_error"] = None
        m["last_error_at"] = None

        # webhooks already disabled in the `scope in ("webhooks","everything")` block,
        # but keep it explicit here for correctness.
        store["sheet_webhook"] = {
            "enabled": False,
            "url": "",
            "name": "Google Sheet receiver",
            "last_push_at": None,
            "last_push_ok": None,
            "last_push_error": None,
            "push_count": 0,
            "push_errors": 0,
        }

    return store


def _match_time_range(value: str | None, *, from_val: str | None, to_val: str | None) -> bool:
    if value is None:
        value = ""
    v = str(value)
    if from_val:
        if v < str(from_val):
            return False
    if to_val:
        if v > str(to_val):
            return False
    return True


def delete_errors(store: dict, filters: dict | None) -> dict:
    """
    Delete subset of errors using filters. Supported:
      - where (exact match, case-insensitive)
      - reason_contains (substring match, case-insensitive)
      - feed_id
      - profile_id
      - at_from / at_to  (ISO-ish string compare, works for exported timestamps)
      - error_id (exact match)
    """
    filters = dict(filters or {})
    errors = list(store.get("errors") or [])

    where = str(filters.get("where") or "").strip().lower()
    reason_contains = str(filters.get("reason_contains") or "").strip().lower()
    feed_id = str(filters.get("feed_id") or "").strip()
    profile_id = str(filters.get("profile_id") or "").strip()
    error_id = str(filters.get("error_id") or "").strip()
    at_from = str(filters.get("at_from") or "").strip() or None
    at_to = str(filters.get("at_to") or "").strip() or None

    def matches(e: dict) -> bool:
        if error_id and str(e.get("error_id") or "").strip() != error_id:
            return False
        if where and str(e.get("where") or "").lower() != where:
            return False
        if reason_contains and reason_contains not in str(e.get("reason") or "").lower():
            return False
        if feed_id and str(e.get("feed_id") or "").strip() != feed_id:
            return False
        if profile_id and str(e.get("profile_id") or "").strip() != profile_id:
            return False
        if at_from or at_to:
            if not _match_time_range(str(e.get("at") or ""), from_val=at_from, to_val=at_to):
                return False
        return True

    remaining = [e for e in errors if not matches(e)]
    removed = len(errors) - len(remaining)
    store["errors"] = remaining[-MAX_ERRORS:]

    m = store.setdefault("metrics", {})
    if store["errors"]:
        newest = max(store["errors"], key=lambda x: str(x.get("at") or ""))
        m["last_error"] = str(newest.get("reason") or "")[:400]
        m["last_error_at"] = newest.get("at")
    else:
        m["last_error"] = None
        m["last_error_at"] = None

    return {"removed": removed, "remaining": len(store["errors"])}


def delete_decisions(store: dict, filters: dict | None) -> dict:
    """
    Delete subset of decision CSV rows.

    Supported (safe/reliable):
      - profile_id: delete all rows for that profile and rebuild its paper account to FLAT.
      - feed_id: delete all rows for that feed.
    If no filters are provided, deletes all decisions (like clear scope "decisions").

    Note: partial time-range deletion is not supported here (requires full replay to fix equity fields).
    """
    filters = dict(filters or {})
    profile_id = str(filters.get("profile_id") or "").strip()
    feed_id = str(filters.get("feed_id") or "").strip()

    # Reject partial range deletion: would leave stale equity/floating_pnl fields.
    for k in ("bar_time_from", "bar_time_to", "received_at_from", "received_at_to"):
        if str(filters.get(k) or "").strip():
            raise ValueError(f"partial time-range deletion not supported for {k}")

    if not profile_id and not feed_id:
        store["decisions"] = []
        store["accounts"] = {}
    else:
        store["decisions"] = [
            d
            for d in (store.get("decisions") or [])
            if not (
                (profile_id and str(d.get("profile_id") or "").strip() == profile_id)
                or (feed_id and str(d.get("feed_id") or "").strip() == feed_id)
            )
        ]

        # Rebuild paper accounts from the latest remaining row per profile.
        store["accounts"] = {}
        profiles_map = {str(p.get("profile_id") or "").strip(): p for p in (store.get("profiles") or []) if p.get("profile_id")}

        latest: dict[str, dict] = {}
        for d in sorted(store.get("decisions") or [], key=lambda r: str(r.get("received_at") or r.get("bar_time") or "")):
            pid = str(d.get("profile_id") or "").strip()
            if pid:
                latest[pid] = d

        for pid, prof in profiles_map.items():
            if pid not in latest:
                continue
            acc = ensure_account(store, prof)
            last = latest[pid]
            mark = float(last.get("mark_price") or last.get("close") or 0)
            n_units = int(float(last.get("open_units") or 0))
            side = str(last.get("position_side") or "FLAT").upper()
            acc["banked_balance"] = float(
                last.get("banked_balance") if last.get("banked_balance") is not None else last.get("start_balance") or prof.get("start_balance") or 0.0
            )
            acc["side"] = side if side in ("LONG", "SHORT") else "FLAT"
            rebuilt_lot = float(last.get("fill_lot") or last.get("lot_size") or prof.get("lot_size") or 0.01)
            acc["units"] = (
                [_make_unit(mark, rebuilt_lot) for _ in range(n_units)]
                if acc["side"] in ("LONG", "SHORT") and n_units > 0 and mark > 0
                else []
            )
            acc["peak_equity"] = float(last.get("peak_equity") or 0.0)
            acc["max_drawdown_pct"] = float(last.get("max_drawdown_pct") or 0.0)
            acc["trades"] = int(float(last.get("account_trades") or 0))
            acc["skips"] = int(float(last.get("account_skips") or 0))
            acc["stopouts"] = int(float(last.get("account_stopouts") or 0))

    # metrics: rebuild decision counters + last timestamps
    recompute_decision_metrics(store)
    m = store.setdefault("metrics", {})
    if store.get("decisions"):
        newest = max((str(r.get("received_at") or "") for r in store.get("decisions") or []), default="")
        m["last_ingest_at"] = newest or None
        m["last_signal_at"] = newest or None
    else:
        m["last_ingest_at"] = None
        m["last_signal_at"] = None

    return {"decisions_remaining": len(store.get("decisions") or [])}


def _pipeline_ids(store: dict) -> tuple[set[str], set[str]]:
    feed_ids = {str(f.get("feed_id")) for f in (store.get("feeds") or []) if f.get("feed_id")}
    profile_ids = {str(p.get("profile_id")) for p in (store.get("profiles") or []) if p.get("profile_id")}
    return feed_ids, profile_ids


def purge_orphan_decisions(store: dict) -> int:
    """Drop decision rows whose feed or profile no longer exists in the lab store."""
    feed_ids, profile_ids = _pipeline_ids(store)
    before = len(store.get("decisions") or [])
    kept: list[dict] = []
    for d in store.get("decisions") or []:
        pid = str(d.get("profile_id") or "")
        fid = str(d.get("feed_id") or "")
        if not pid or pid not in profile_ids:
            continue
        if fid and fid not in feed_ids:
            continue
        kept.append(d)
    store["decisions"] = kept[-MAX_DECISIONS:]
    return before - len(store["decisions"])


def purge_stale_accounts(store: dict) -> None:
    profile_ids = {str(p.get("profile_id")) for p in (store.get("profiles") or []) if p.get("profile_id")}
    accounts = dict(store.get("accounts") or {})
    for pid in list(accounts.keys()):
        if pid not in profile_ids:
            del accounts[pid]
    store["accounts"] = accounts


def recompute_decision_metrics(store: dict) -> None:
    dec = list(store.get("decisions") or [])
    m = store.setdefault("metrics", {})
    m["decisions_total"] = len(dec)
    actions: dict[str, int] = {}
    for d in dec:
        sig = str(d.get("signal") or "NONE").upper()
        actions[sig] = int(actions.get(sig) or 0) + 1
    m["actions"] = actions
    if dec:
        newest = max(str(d.get("received_at") or "") for d in dec)
        if newest:
            m["last_ingest_at"] = newest
            m["last_signal_at"] = newest
    else:
        m["last_ingest_at"] = None
        m["last_signal_at"] = None


def on_pipeline_deleted(store: dict) -> dict:
    """
    After a feed/profile is removed:
      - purge orphan CSV rows + stale paper accounts
      - if no feeds or no profiles remain, wipe all output (fresh start for new pipeline)
    """
    removed = purge_orphan_decisions(store)
    purge_stale_accounts(store)
    feeds = store.get("feeds") or []
    profiles = store.get("profiles") or []
    wiped = False
    if not feeds or not profiles:
        store["decisions"] = []
        store["accounts"] = {}
        wiped = True
        # Reset throughput counters so Home dashboard shows 0 after full pipeline delete.
        m = store.setdefault("metrics", {})
        for k in (
            "ingest_total",
            "ingest_errors",
            "signal_total",
            "signal_errors",
            "warmup_total",
            "reset_total",
        ):
            m[k] = 0
        m["actions"] = {}
        m["latencies_ms"] = []
        m["last_ingest_at"] = None
        m["last_signal_at"] = None
        m["last_error"] = None
        m["last_error_at"] = None
    recompute_decision_metrics(store)
    return {
        "removed_decisions": removed,
        "pipeline_wiped": wiped,
        "decisions_remaining": len(store.get("decisions") or []),
        "needs_model_reset": wiped or not feeds,
    }


def delete_profile(store: dict, profile_id: str) -> tuple[bool, dict]:
    pid = (profile_id or "").strip()
    before = len(store.get("profiles") or [])
    store["profiles"] = [p for p in (store.get("profiles") or []) if p.get("profile_id") != pid]
    if pid in (store.get("accounts") or {}):
        del store["accounts"][pid]
    store["decisions"] = [d for d in (store.get("decisions") or []) if d.get("profile_id") != pid]
    for f in store.get("feeds") or []:
        linked = list(f.get("linked_profile_ids") or [])
        f["linked_profile_ids"] = [x for x in linked if x != pid]
    cleanup = on_pipeline_deleted(store)
    return len(store.get("profiles") or []) < before, cleanup


def delete_feed(store: dict, feed_id: str) -> tuple[bool, dict]:
    fid = (feed_id or "").strip()
    before = len(store.get("feeds") or [])
    store["feeds"] = [f for f in (store.get("feeds") or []) if f.get("feed_id") != fid]
    store["decisions"] = [d for d in (store.get("decisions") or []) if d.get("feed_id") != fid]
    cleanup = on_pipeline_deleted(store)
    return len(store.get("feeds") or []) < before, cleanup


def ensure_store_shape(store: dict, model_name: str) -> dict:
    """Backfill fields for older S3 stores."""
    if "sheet_webhook" not in store:
        store["sheet_webhook"] = default_store(model_name)["sheet_webhook"]
    if "decisions" not in store:
        store["decisions"] = []
    if "errors" not in store:
        store["errors"] = []
    if "profiles" not in store:
        store["profiles"] = []
    if "feeds" not in store:
        store["feeds"] = []
    purge_orphan_decisions(store)
    purge_stale_accounts(store)
    recompute_decision_metrics(store)
    return store


def analysis_payload(store: dict) -> dict:
    """Per-profile time series for Analysis charts."""
    sections = []
    for p in store.get("profiles") or []:
        if p.get("enabled") is False:
            continue
        pid = p["profile_id"]
        rows = [d for d in (store.get("decisions") or []) if d.get("profile_id") == pid]
        # chronological
        rows = sorted(rows, key=lambda r: str(r.get("bar_time") or r.get("received_at") or ""))
        start = float(p.get("start_balance") or 0)
        points = []
        for d in rows:
            t = d.get("bar_time") or d.get("received_at")
            banked = d.get("banked_balance")
            flot = d.get("floating_pnl")
            eq = d.get("equity")
            realized = d.get("realized_pnl")
            profit = None if eq is None else round(float(eq) - start, 4)
            points.append(
                {
                    "t": t,
                    "banked_balance": banked,
                    "floating_pnl": flot,
                    "equity": eq,
                    "profit": profit,
                    "free_margin": d.get("free_margin"),
                    "margin_level_pct": d.get("margin_level_pct"),
                    "signal": d.get("signal"),
                    "status_note": d.get("status_note"),
                    "open_units": d.get("open_units"),
                }
            )
        acc = (store.get("accounts") or {}).get(pid) or {}
        last = points[-1] if points else None
        sections.append(
            {
                "profile_id": pid,
                "profile_name": p.get("profile_name"),
                "start_balance": start,
                "lot_size": p.get("lot_size"),
                "leverage": p.get("leverage"),
                "webhook_url": p.get("webhook_url") or "",
                "points": points,
                "latest": last,
                "peak_equity": acc.get("peak_equity"),
                "max_drawdown_pct": acc.get("max_drawdown_pct"),
                "trades": acc.get("trades", 0),
                "skips": acc.get("skips", 0),
                "stopouts": acc.get("stopouts", 0),
                "n_points": len(points),
            }
        )
    return {
        "model_name": store.get("model_name"),
        "sections": sections,
        "sheet_webhook": store.get("sheet_webhook") or {},
    }


def push_rows_to_webhooks(store: dict, rows: list[dict]) -> list[dict]:
    """POST decision rows to the Apps Script URL in store['sheet_webhook']."""
    """
    Push decision rows to:
      1) global sheet_webhook.url (if enabled) — ONE webhook for all profiles → one Sheet
      2) optional per-profile webhook_url
    Returns list of push results.
    """
    import urllib.error
    import urllib.request

    results = []
    sw = store.get("sheet_webhook") or {}
    targets = []

    if sw.get("enabled") and (sw.get("url") or "").strip():
        targets.append(("sheet", sw["url"].strip(), rows))

    # group by per-profile override
    by_prof = {}
    for r in rows:
        pid = r.get("profile_id")
        prof = next((p for p in (store.get("profiles") or []) if p.get("profile_id") == pid), None)
        url = (prof or {}).get("webhook_url") or ""
        if url.strip():
            by_prof.setdefault(url.strip(), []).append(r)
    for url, rs in by_prof.items():
        targets.append(("profile", url, rs))

    for kind, url, rs in targets:
        payload = {
            "source": "xauusd_lab",
            "model_name": store.get("model_name"),
            "pushed_at": _now(),
            "count": len(rs),
            "rows": rs,
        }
        # also send flat first row fields for simple Apps Script that expects one object
        if len(rs) == 1:
            payload.update(rs[0])
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                code = resp.status
                text = resp.read().decode("utf-8", errors="replace")[:300]
            ok = 200 <= code < 300
            # Apps Script often returns HTTP 200 with {"success":false,...}
            if ok and text:
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict) and "success" in parsed and parsed.get("success") is False:
                        ok = False
                except Exception:
                    pass
            results.append({"kind": kind, "url": url, "ok": ok, "code": code, "body": text})
            if kind == "sheet":
                sw["last_push_at"] = _now()
                sw["last_push_ok"] = ok
                sw["last_push_error"] = None if ok else text
                sw["push_count"] = int(sw.get("push_count") or 0) + 1
                if not ok:
                    sw["push_errors"] = int(sw.get("push_errors") or 0) + 1
            if not ok:
                append_error(
                    store,
                    where="webhook",
                    reason=f"{kind} webhook HTTP {code}",
                    detail=text,
                    http_status=code,
                    profile_id=(rs[0].get("profile_id") if rs else None),
                    feed_id=(rs[0].get("feed_id") if rs else None),
                )
        except Exception as e:
            results.append({"kind": kind, "url": url, "ok": False, "error": str(e)})
            if kind == "sheet":
                sw["last_push_at"] = _now()
                sw["last_push_ok"] = False
                sw["last_push_error"] = str(e)[:400]
                sw["push_errors"] = int(sw.get("push_errors") or 0) + 1
            append_error(
                store,
                where="webhook",
                reason=f"{kind} webhook exception",
                detail=str(e)[:400],
                profile_id=(rs[0].get("profile_id") if rs else None),
                feed_id=(rs[0].get("feed_id") if rs else None),
            )

    store["sheet_webhook"] = sw
    return results


def load_store(model_name: str) -> dict:
    """Read lab json from S3 (or /tmp). Creates a default store on first run."""
    bucket = _bucket()
    if bucket:
        try:
            import boto3

            obj = boto3.client("s3").get_object(Bucket=bucket, Key=_key(model_name))
            store = json.loads(obj["Body"].read().decode("utf-8"))
            store = ensure_store_shape(store, model_name)
            # Remembered so save_store can compare-and-swap against exactly the
            # version this invocation read.
            store["_etag"] = (obj.get("ETag") or "").strip('"') or None
            return store
        except Exception as e:
            if "NoSuchKey" not in str(e) and "404" not in str(e) and "Not Found" not in str(e):
                pass
    if TMP_PATH.is_file():
        try:
            return ensure_store_shape(json.loads(TMP_PATH.read_text(encoding="utf-8")), model_name)
        except Exception:
            pass
    return default_store(model_name)
