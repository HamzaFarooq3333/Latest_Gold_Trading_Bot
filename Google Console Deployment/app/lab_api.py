"""Lab + engine API routes for GCP (adapted from lambda_asim_fb_agent/lambda_function.py)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

import lab_core as lab
from server import Candle, WarmupReq, health, reset, signal, warmup

HERE = Path(__file__).resolve().parent
ATR_MIN_BARS = 14
MIN_WARMUP_BARS = 50
DEFAULT_WARMUP_BARS = MIN_WARMUP_BARS * 2


def model_name() -> str:
    return os.environ.get("MODEL_NAME", "Asim GCP Live")


def load_warmup_pack() -> list:
    return json.loads((HERE / "warmup_pack.json").read_text(encoding="utf-8"))


def _json_response(status: int, payload: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=payload,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
    )


def proxy(method: str, path: str, body: dict | None = None, timeout: int = 25):
    """Call the co-located engine (server.py) directly — no HTTP hop."""
    del timeout
    t0 = time.perf_counter()
    try:
        if method == "GET" and path == "/health":
            data = health()
            return 200, data, round((time.perf_counter() - t0) * 1000, 1)
        if method == "POST" and path == "/reset":
            data = reset(x_api_key=None)
            return 200, data, round((time.perf_counter() - t0) * 1000, 1)
        if method == "POST" and path == "/warmup":
            candles = [Candle(**c) if isinstance(c, dict) else c for c in (body or {}).get("candles", [])]
            data = warmup(WarmupReq(candles=candles), x_api_key=None)
            return 200, data, round((time.perf_counter() - t0) * 1000, 1)
        if method == "POST" and path == "/signal":
            data = signal(Candle(**(body or {})), x_api_key=None)
            return 200, data, round((time.perf_counter() - t0) * 1000, 1)
        return 404, {"error": "not found", "path": path}, round((time.perf_counter() - t0) * 1000, 1)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, (dict, list)) else {"error": str(e.detail)}
        return e.status_code, detail, round((time.perf_counter() - t0) * 1000, 1)
    except Exception as e:
        return 502, {"error": str(e)}, round((time.perf_counter() - t0) * 1000, 1)


async def _read_body(request: Request) -> tuple[dict, str, dict]:
    raw = await request.body()
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    for k, v in request.query_params.items():
        parsed.setdefault(k, v)
    headers = {k.lower(): v for k, v in request.headers.items()}
    return parsed, text, headers


def handle_lab(method: str, path: str, body: dict, raw_text: str, headers: dict):
    name = model_name()

    if method == "GET" and path in ("/api/lab/dataset", "/api/lab/dataset/"):
        return _json_response(200, {"ok": True, **lab.dataset_meta(name)})

    if method == "GET" and path == "/api/lab/dataset.csv":
        text, meta = lab.load_dataset_csv(name)
        if text is None:
            return _json_response(404, {"error": "no saved dataset"})
        fname = str(meta.get("filename") or "dataset.csv").replace('"', "")
        return Response(
            content=text,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'inline; filename="{fname}"',
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )

    if method in ("PUT", "POST") and path == "/api/lab/dataset":
        ctype = headers.get("content-type", "").lower()
        filename = headers.get("x-filename") or "dataset.csv"
        csv_text = ""
        if "application/json" in ctype:
            csv_text = str(body.get("csv") or body.get("text") or "")
            filename = str(body.get("filename") or filename)
        else:
            csv_text = raw_text
        try:
            meta = lab.save_dataset_csv(name, csv_text, filename)
        except ValueError as e:
            return _json_response(400, {"error": str(e)})
        return _json_response(200, {"ok": True, **meta})

    store = lab.load_store(name)

    if method == "GET" and path == "/api/lab/state":
        return _json_response(
            200,
            {
                "ok": True,
                "model_name": name,
                "feeds": store.get("feeds") or [],
                "profiles": store.get("profiles") or [],
                "decisions": store.get("decisions") or [],
                "errors": store.get("errors") or [],
                "sheet_webhook": store.get("sheet_webhook") or {},
                "metrics": lab.metrics_summary(store),
                "updated_at": store.get("updated_at"),
            },
        )

    if method == "GET" and path == "/api/lab/analysis":
        return _json_response(200, {"ok": True, **lab.analysis_payload(store)})

    if method == "GET" and path == "/api/lab/metrics":
        return _json_response(200, {"ok": True, "metrics": lab.metrics_summary(store)})

    if method == "GET" and path == "/api/lab/decisions":
        return _json_response(200, {"ok": True, "decisions": store.get("decisions") or []})

    if method == "GET" and path == "/api/lab/errors":
        return _json_response(200, {"ok": True, "errors": store.get("errors") or []})

    if method == "DELETE" and path == "/api/lab/errors":
        info = lab.delete_errors(store, body)
        lab.save_store(name, store)
        return _json_response(200, {"ok": True, **info})

    if method == "DELETE" and path == "/api/lab/decisions":
        info = lab.delete_decisions(store, body)
        lab.save_store(name, store)
        return _json_response(200, {"ok": True, **info})

    if method == "POST" and path == "/api/lab/errors":
        reason = str(body.get("reason") or "").strip()
        if not reason:
            return _json_response(400, {"error": "reason required"})
        row = lab.append_error(
            store,
            where=str(body.get("where") or "watchdog"),
            reason=reason,
            detail=str(body.get("detail") or "")[:800],
            feed_id=(body.get("feed_id") or None),
            profile_id=(body.get("profile_id") or None),
            http_status=body.get("http_status"),
            latency_ms=body.get("latency_ms"),
        )
        m = store.setdefault("metrics", {})
        m["last_error"] = row["reason"][:400]
        m["last_error_at"] = row["at"]
        lab.save_store(name, store)
        return _json_response(200, {"ok": True, "error": row})

    if method == "PUT" and path == "/api/lab/webhook":
        sw = dict(store.get("sheet_webhook") or {})
        if "url" in body:
            sw["url"] = str(body.get("url") or "").strip()
        if "enabled" in body:
            sw["enabled"] = bool(body.get("enabled"))
        if "name" in body:
            sw["name"] = str(body.get("name") or sw.get("name") or "Google Sheet receiver")
        store["sheet_webhook"] = sw
        lab.save_store(name, store)
        return _json_response(200, {"ok": True, "sheet_webhook": sw})

    if method == "DELETE" and path == "/api/lab/profiles":
        pid = (body.get("profile_id") or "").strip()
        if not pid:
            return _json_response(400, {"error": "profile_id required"})
        ok, cleanup = lab.delete_profile(store, pid)
        if cleanup.get("needs_model_reset"):
            proxy("POST", "/reset", {}, timeout=30)
            lab.record_metric(store, "reset")
        lab.save_store(name, store)
        return _json_response(200 if ok else 404, {"ok": ok, "profile_id": pid, "cleanup": cleanup})

    if method == "DELETE" and path == "/api/lab/feeds":
        fid = (body.get("feed_id") or "").strip()
        if not fid:
            return _json_response(400, {"error": "feed_id required"})
        ok, cleanup = lab.delete_feed(store, fid)
        if cleanup.get("needs_model_reset"):
            proxy("POST", "/reset", {}, timeout=30)
            lab.record_metric(store, "reset")
        lab.save_store(name, store)
        return _json_response(200 if ok else 404, {"ok": ok, "feed_id": fid, "cleanup": cleanup})

    if method == "POST" and path == "/api/lab/clear":
        if str(body.get("password") or "") != lab.CLEAR_PASSWORD:
            return _json_response(403, {"error": "wrong password"})
        scope = str(body.get("scope") or "records").strip().lower()
        try:
            lab.clear_lab_data(store, name, scope)
        except ValueError as e:
            return _json_response(400, {"error": str(e)})
        model_reset = None
        warmup_info = None
        if scope in ("decisions", "records", "everything"):
            code, data, ms = proxy("POST", "/reset", {}, timeout=30)
            lab.record_metric(store, "reset", ms=ms)
            model_reset = {
                "ok": 200 <= int(code or 0) < 300,
                "http_status": code,
                "latency_ms": ms,
                "upstream": data if isinstance(data, dict) else {"raw": str(data)[:200]},
            }
            try:
                candles, meta = lab.fetch_gold_ohlc_candles(DEFAULT_WARMUP_BARS)
                wcode, wdata, wms = proxy("POST", "/warmup", {"candles": candles}, timeout=25)
                lab.record_metric(store, "warmup", ms=wms)
                warmup_info = {
                    "ok": 200 <= int(wcode or 0) < 300,
                    "http_status": wcode,
                    "latency_ms": wms,
                    "source": "gold_ohlc",
                    **meta,
                    "upstream": wdata if isinstance(wdata, dict) else {"raw": str(wdata)[:200]},
                }
            except Exception as e:
                warmup_info = {"ok": False, "source": "gold_ohlc", "error": str(e)[:300]}
        lab.save_store(name, store)
        return _json_response(
            200,
            {"ok": True, "scope": scope, "cleared": True, "model_reset": model_reset, "warmup": warmup_info},
        )

    if method == "POST" and path == "/api/lab/seed-demo":
        n_bars = max(8, min(int(body.get("n_bars") or 24), 80))
        try:
            info = lab.seed_demo_trades(store, name, n_bars=n_bars)
        except Exception as e:
            return _json_response(400, {"error": str(e)})
        lab.save_store(name, store)
        return _json_response(200, {"ok": True, "seeded": True, **info})

    if method == "POST" and path == "/api/lab/import-decisions":
        rows = body.get("rows")
        if not isinstance(rows, list):
            return _json_response(400, {"error": "rows must be a list of decision objects"})
        try:
            info = lab.import_sheet_decisions(store, rows, replace=bool(body.get("replace", True)))
        except Exception as e:
            return _json_response(400, {"error": str(e)})
        lab.save_store(name, store)
        return _json_response(200, {"ok": True, "imported": True, **info})

    if method == "PUT" and path == "/api/lab/feeds":
        fid = (body.get("feed_id") or "").strip()
        if not fid:
            return _json_response(400, {"error": "feed_id required"})
        feeds = list(store.get("feeds") or [])
        found = False
        for i, f in enumerate(feeds):
            if f.get("feed_id") == fid:
                feeds[i] = {**f, **body, "feed_id": fid}
                found = True
                break
        if not found:
            feeds.append(body)
        store["feeds"] = feeds
        lab.save_store(name, store)
        return _json_response(200, {"ok": True, "feed": body})

    if method == "PUT" and path == "/api/lab/profiles":
        pid = (body.get("profile_id") or "").strip()
        old = (body.get("old_profile_id") or pid).strip()
        if not pid:
            return _json_response(400, {"error": "profile_id required"})
        body.pop("old_profile_id", None)
        body["model_name"] = body.get("model_name") or name
        profiles = list(store.get("profiles") or [])
        found = False
        for i, p in enumerate(profiles):
            if p.get("profile_id") == old:
                profiles[i] = {**p, **body, "profile_id": pid}
                found = True
                break
        if not found:
            profiles.append(body)
        if old != pid:
            for f in store.get("feeds") or []:
                linked = list(f.get("linked_profile_ids") or [])
                f["linked_profile_ids"] = [pid if x == old else x for x in linked]
            if old in (store.get("accounts") or {}):
                store["accounts"][pid] = store["accounts"].pop(old)
        store["profiles"] = profiles
        lab.save_store(name, store)
        return _json_response(200, {"ok": True, "profile": body})

    if method == "POST" and path == "/api/lab/ingest":
        return _handle_ingest(store, name, body)

    return None


def _handle_ingest(store: dict, name: str, body: dict):
    feed_id = (body.get("feed_id") or "").strip()
    feed = next((f for f in (store.get("feeds") or []) if f.get("feed_id") == feed_id), None)
    if not feed:
        lab.record_metric(
            store, "ingest_error", error=f"unknown feed_id {feed_id}", where="ingest",
            detail="feed_id not found in lab store", feed_id=feed_id, http_status=404,
        )
        lab.save_store(name, store)
        return _json_response(404, {"error": f"unknown feed_id: {feed_id}"})
    if feed.get("enabled") is False:
        return _json_response(400, {"error": "feed disabled"})

    profile_ids = list(body.get("profile_ids") or feed.get("linked_profile_ids") or [])
    profiles = [p for p in (store.get("profiles") or []) if p.get("profile_id") in profile_ids and p.get("enabled") is not False]
    if not profile_ids or not profiles:
        lab.record_metric(
            store, "ingest_error", error="no linked output profiles", where="ingest",
            detail="link at least one enabled output profile to this feed before ingest",
            feed_id=feed_id, http_status=400,
        )
        lab.save_store(name, store)
        return _json_response(400, {"ok": False, "skipped": True, "error": "no output profiles linked to this feed", "feed_id": feed_id, "rows": [], "model_signal": None})

    need = ["open", "high", "low", "close", "xtrend", "hist"]
    if any(body.get(k) is None or body.get(k) == "" for k in need):
        return _json_response(400, {"error": "missing OHLC/xtrend/hist"})

    candle = {k: float(body[k]) for k in need}
    raw_keys = ["raw_open", "raw_high", "raw_low", "raw_close"]
    raw_present = [body.get(k) is not None and body.get(k) != "" for k in raw_keys]
    if any(raw_present) and not all(raw_present):
        return _json_response(400, {"error": "raw_open/raw_high/raw_low/raw_close must be sent together"})
    if all(raw_present):
        candle.update({k: float(body[k]) for k in raw_keys})
    if body.get("bar_time"):
        candle["time"] = str(body["bar_time"])
    hc = str(body.get("histcolor") or body.get("hist_color") or "").strip().lower()
    if hc in ("green", "red", "orange"):
        candle["histcolor"] = hc

    code, data, ms = proxy("POST", "/signal", candle, timeout=20)
    if code < 200 or code >= 300 or not isinstance(data, dict):
        lab.record_metric(store, "signal_error", ms=ms, error=str(data)[:300], where="signal", detail=f"model HTTP {code} on /signal", feed_id=feed_id, http_status=code or 502)
        lab.record_metric(store, "ingest_error", error=f"model HTTP {code}", where="ingest", detail=str(data)[:300], feed_id=feed_id, http_status=code or 502, ms=ms)
        lab.save_store(name, store)
        return _json_response(code or 502, {"error": "model signal failed", "upstream": data, "model_latency_ms": ms})

    model_signal = str(data.get("action") or "NONE")
    lab.record_metric(store, "signal", ms=ms, action=model_signal)
    lab.record_metric(store, "ingest", ms=ms)

    if data.get("duplicate_bar"):
        lab.save_store(name, store)
        return _json_response(200, {"ok": True, "duplicate_bar": True, "feed_id": feed_id, "model_signal": model_signal, "model_latency_ms": ms, "model_response": data, "rows": [], "webhook_pushes": []})

    histcolor = lab.resolve_histcolor(body, data, candle["hist"])
    xtrend_touching = lab.resolve_xtrend_touching(data, candle)
    price = float(body.get("raw_close") or body["close"])
    order_hint = data.get("order")
    if isinstance(order_hint, dict) and isinstance(order_hint.get("orders"), list):
        broker_orders = order_hint["orders"]
    elif isinstance(order_hint, dict):
        broker_orders = [order_hint]
    else:
        broker_orders = []

    model_events = []
    filled = data.get("filled_action")
    if filled in ("BUY", "SELL", "BUY_ADD", "SELL_ADD"):
        fill_px = data.get("fill_price") or data.get("raw_open") or candle.get("raw_open") or body["open"]
        model_events.append({"action": filled, "price": float(fill_px), "lot": data.get("fill_lot"), "reason": "prev_body_fill"})
    closed_units = list(data.get("closed_units") or [])
    if not closed_units:
        if data.get("closed_primary"):
            closed_units.append({"kind": "primary", **data["closed_primary"]})
        for stopped in data.get("closed_supps") or []:
            closed_units.append({"kind": "supp", **stopped})
    for closed in closed_units:
        model_events.append({"action": "EXIT_PRIMARY" if closed.get("kind") == "primary" else "EXIT_ONE", "price": float(closed["exit"]), "entry_hint": closed.get("entry"), "lot": closed.get("lot"), "reason": closed.get("reason") or "trailing_stop"})
    if model_signal == "EXIT":
        model_events.append({"action": "EXIT", "price": price, "reason": "engine_flatten"})
    if not model_events:
        model_events.append({"action": model_signal, "price": price, "reason": "mark"})

    rows = []
    for p in profiles:
        snap = None
        realized_total = 0.0
        for model_event in model_events:
            snap = lab.apply_paper_action(store, p, model_event["action"], float(model_event["price"]), entry_hint=model_event.get("entry_hint"), lot_override=model_event.get("lot"))
            if snap.get("realized_pnl") is not None:
                realized_total += float(snap["realized_pnl"])
        if snap and snap.get("open_units"):
            snap = lab.apply_paper_action(store, p, "NONE", price)
        snap = dict(snap or {})
        snap["realized_pnl"] = round(realized_total, 4) if realized_total else None
        row = {
            "received_at": lab._now(),
            "feed_id": feed.get("feed_id"),
            "feed_name": feed.get("feed_name"),
            "profile_id": p.get("profile_id"),
            "profile_name": p.get("profile_name"),
            "model_name": name,
            "bar_time": body.get("bar_time") or candle.get("time"),
            "source": "webhook",
            "signal": snap.get("signal"),
            "model_raw_action": model_signal,
            "model_events": model_events,
            "broker_orders": broker_orders,
            "histcolor": candle.get("histcolor") or data.get("histcolor") or histcolor,
            "xtrend_touching": xtrend_touching,
            "open": candle["open"], "high": candle["high"], "low": candle["low"], "close": candle["close"],
            "mark_price": candle["close"], "xtrend": candle["xtrend"], "hist": candle["hist"],
            "sl": data.get("sl"), "model_latency_ms": ms, **snap,
        }
        lab.append_decision(store, row)
        rows.append(row)

    lab.save_store(name, store)
    push_results = lab.push_rows_to_webhooks(store, rows) if rows else []
    lab.save_store(name, store)
    return _json_response(
        200 if profiles else 400,
        {"ok": bool(profiles), "feed_id": feed_id, "model_signal": model_signal, "model_latency_ms": ms, "model_response": data, "rows": rows, "webhook_pushes": push_results},
    )


async def handle_engine_api(method: str, path: str, body: dict):
    name = model_name()

    if method == "GET" and path == "/api/warmup-pack":
        pack = load_warmup_pack()
        return _json_response(200, {"candles": pack, "n": len(pack)})

    if method == "GET" and path == "/api/health":
        code, data, ms = proxy("GET", "/health", timeout=30)
        connection_ok = code != 502 and not (isinstance(data, dict) and "error" in data and code >= 500)
        if isinstance(data, dict) and code < 500:
            out = dict(data)
        else:
            out = {"ok": False, "model": None, "mode": None, "warmed_up": False, "position": None, "n_units": None, "error": data.get("error") if isinstance(data, dict) else str(data)[:300]}
        out["connection_ok"] = connection_ok
        out["latency_ms"] = ms
        out["upstream"] = "local"
        out["upstream_http_status"] = code
        out["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        out["model_name"] = name
        return _json_response(200 if connection_ok else 502, out)

    if method == "POST" and path == "/api/reset":
        code, data, ms = proxy("POST", "/reset", {}, timeout=30)
        store = lab.load_store(name)
        lab.record_metric(store, "reset", ms=ms)
        lab.save_store(name, store)
        return _json_response(code, data)

    if method == "POST" and path == "/api/warmup":
        candles = body.get("candles")
        source = "body"
        source_meta: dict = {}
        if not candles:
            n = int(body.get("n", DEFAULT_WARMUP_BARS))
            try:
                candles, source_meta = lab.fetch_gold_ohlc_candles(n)
                source = "gold_ohlc"
            except Exception as e:
                candles = load_warmup_pack()[-n:]
                source = "pack"
                source_meta = {"source": "pack", "fed": len(candles), "fallback_error": str(e)[:300]}
        if body.get("reset_first", True):
            proxy("POST", "/reset", {}, timeout=30)
        code, data, ms = proxy("POST", "/warmup", {"candles": candles}, timeout=25)
        store = lab.load_store(name)
        lab.record_metric(store, "warmup", ms=ms)
        lab.save_store(name, store)
        out = data if isinstance(data, dict) else {"upstream": data}
        if isinstance(out, dict):
            out = {**out, "ok": out.get("ok", 200 <= code < 300), "source": source, "warmup_source": source, "latency_ms": ms, **{k: v for k, v in source_meta.items() if k not in out}}
        return _json_response(code, out)

    if method == "POST" and path == "/api/signal":
        code, data, ms = proxy("POST", "/signal", body, timeout=20)
        store = lab.load_store(name)
        if 200 <= code < 300 and isinstance(data, dict):
            lab.record_metric(store, "signal", ms=ms, action=str(data.get("action") or ""))
        else:
            lab.record_metric(store, "signal_error", ms=ms, error=str(data)[:300], where="signal", detail=f"direct /api/signal failed HTTP {code}", http_status=code or 502)
        lab.save_store(name, store)
        return _json_response(code, data)

    return None


async def dispatch_api(request: Request):
    method = request.method.upper()
    path = request.url.path
    body, raw_text, headers = await _read_body(request)

    if path.startswith("/api/lab/"):
        out = handle_lab(method, path, body, raw_text, headers)
        if out is not None:
            return out

    if path.startswith("/api/") and path not in ("/api/info", "/api/backtest-reference", "/api/auth/login", "/api/auth/logout", "/api/auth/me"):
        if path.startswith("/api/broker/"):
            return None
        out = await handle_engine_api(method, path, body)
        if out is not None:
            return out
        if path.startswith("/api/lab/") or path in ("/api/health", "/api/reset", "/api/warmup", "/api/signal", "/api/warmup-pack"):
            return _json_response(404, {"error": "not found", "path": path})

    return None
