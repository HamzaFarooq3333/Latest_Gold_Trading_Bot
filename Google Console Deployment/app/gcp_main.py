"""Asim live desk on Google Compute Engine — API + frontend."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import auth
import broker_live
import lab_core as lab
from lab_api import dispatch_api
from server import app

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
MODEL_NAME = os.environ.get("MODEL_NAME", "Live Account")
DESK_TITLE = os.environ.get("DESK_TITLE", MODEL_NAME).strip() or MODEL_NAME
EXPECTED_MT5_SYMBOL = os.environ.get("EXPECTED_MT5_SYMBOL", "XAUUSDm")
EXPECTED_MT5_LOGIN = os.environ.get("EXPECTED_MT5_LOGIN", "")
AUTH_USER = os.environ.get("AUTH_USER", "")

PUBLIC_PATHS = {
    "/login",
    "/api/auth/login",
    "/health",
}
PUBLIC_PREFIXES = (
    "/api/broker/",
    "/static/",
)

BACKTEST_REFERENCE = {
    "profile": "asim_aws_histogram_filters_off",
    "engine_source": "lambda_asim_fb_agent/server.py",
    "dataset_simple_gold": "VANTAGE_XAUUSD_15_with_histcolor.csv",
    "dataset_goldm": "XAUUSDm_M15_202601012300_202608271930.csv",
    "policy": {
        "entry": "prev_body_cross_xt",
        "fill": "prev_body_immediate",
        "stop_active_on_entry_bar": False,
        "trail_every_candle": True,
        "tsl_points": 0.25,
        "stop_slippage_points": 0.25,
        "spread_cost_per_fill": 0.06,
        "skip_worst_hours": False,
        "focus_best_hours": False,
        "xtrend_gate": True,
        "xtrend_buffer": 0.0,
        "exit_orange_histogram": True,
    },
    "results_500_simple_gold_filters_off": {
        "start_balance": 500.0,
        "final_equity": 45746.59,
        "net_profit": 45246.59,
        "closed_trades": 7323,
        "wins": 5540,
        "losses": 1783,
        "win_rate_pct": 75.65,
        "primary_entries": 1331,
        "supplementary_entries": 5993,
    },
    "results_500_simple_gold_filters_on": {
        "start_balance": 500.0,
        "final_equity": 57265.40,
        "net_profit": 56765.40,
        "closed_trades": 6085,
        "win_rate_pct": 76.20,
    },
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _path_is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def _login_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url=f"/login?next={quote(path)}", status_code=302)


@app.middleware("http")
async def lab_api_middleware(request: Request, call_next):
    path = request.url.path
    lab_paths = path.startswith("/api/lab/") or path in {
        "/api/health", "/api/reset", "/api/warmup", "/api/signal", "/api/warmup-pack",
    }
    if lab_paths:
        out = await dispatch_api(request)
        if out is not None:
            return out
    return await call_next(request)


@app.middleware("http")
async def session_auth_middleware(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or _path_is_public(path):
        return await call_next(request)

    user = auth.verify_session_token(request.cookies.get(auth.SESSION_COOKIE, ""))
    if user:
        request.state.auth_user = user
        return await call_next(request)

    accept = request.headers.get("accept", "")
    if path.startswith("/api/") or "application/json" in accept:
        return JSONResponse(status_code=401, content={"ok": False, "error": "auth_required"})
    return _login_redirect(path)


def _json_response(status: int, payload: dict):
    return JSONResponse(
        status_code=status,
        content=payload,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
            # Live desk polls every ~2s — never let browsers/proxies cache broker state.
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


def _lambda_response(status: int, payload, content_type: str = "application/json"):
    if content_type.startswith("text/html"):
        return HTMLResponse(content=str(payload), status_code=status)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"raw": payload}
    return _json_response(status, payload)


def _broker_handle(method: str, path: str, body: dict | None = None):
    event = {"body": json.dumps(body or {})}
    out = broker_live.handle(
        method,
        path,
        event,
        lab=lab,
        model_name=MODEL_NAME,
        response=_lambda_response,
    )
    if out is None:
        return _json_response(404, {"error": "not found", "path": path})
    # broker_live.response is _lambda_response → already a Starlette response
    if isinstance(out, (JSONResponse, HTMLResponse)):
        return out
    if out.get("headers", {}).get("Content-Type", "").startswith("text/html"):
        return HTMLResponse(content=out["body"], status_code=out["statusCode"])
    try:
        payload = json.loads(out["body"])
    except (TypeError, json.JSONDecodeError):
        payload = {"raw": out.get("body")}
    return _json_response(out["statusCode"], payload)


@app.get("/login")
def login_page():
    html = (STATIC / "login.html").read_text(encoding="utf-8")
    html = (
        html.replace("__DESK_TITLE__", DESK_TITLE)
        .replace("__MODEL_NAME__", MODEL_NAME)
        .replace("__AUTH_USER__", AUTH_USER or "user")
    )
    return HTMLResponse(html)


@app.post("/api/auth/login")
def auth_login(body: dict = Body(default_factory=dict)):
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    remember = bool(body.get("remember"))
    if not auth.check_credentials(username, password):
        return JSONResponse(status_code=401, content={"ok": False, "error": "Invalid username or password"})
    token, ttl = auth.create_session_token(username, remember=remember)
    resp = JSONResponse(content={"ok": True, "user": username, "remember": remember})
    resp.set_cookie(
        key=auth.SESSION_COOKIE,
        value=token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=request_is_https_hint(),
    )
    return resp


def request_is_https_hint() -> bool:
    # Cookie Secure flag when served behind nginx TLS terminator.
    return os.environ.get("BEHIND_HTTPS", "1") == "1"


@app.post("/api/auth/logout")
def auth_logout():
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = getattr(request.state, "auth_user", None)
    if not user:
        return JSONResponse(status_code=401, content={"ok": False})
    return {"ok": True, "user": user}


@app.get("/")
@app.get("/index.html")
def backtest_page():
    html = (STATIC / "dashboard.html").read_text(encoding="utf-8")
    return HTMLResponse(
        html.replace("__MODEL_NAME__", MODEL_NAME),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/live")
def live_page():
    html = (STATIC / "live_dashboard.html").read_text(encoding="utf-8")
    html = (
        html.replace("__DESK_TITLE__", DESK_TITLE)
        .replace("__MODEL_NAME__", MODEL_NAME)
        .replace("__EXPECTED_MT5_SYMBOL__", EXPECTED_MT5_SYMBOL)
        .replace("__EXPECTED_MT5_LOGIN__", EXPECTED_MT5_LOGIN)
        .replace("__AUTH_USER__", AUTH_USER or "")
    )
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/api/info")
def api_info():
    return {
        "ok": True,
        "platform": "google-compute-engine",
        "model_name": MODEL_NAME,
        "desk_title": DESK_TITLE,
        "auth_user": AUTH_USER or None,
        "expected_login": EXPECTED_MT5_LOGIN or None,
        "expected_symbol": EXPECTED_MT5_SYMBOL,
        "live_path": "/live",
        "backtest_path": "/",
        "engine_health": "/health",
        "broker_state": "/api/broker/state",
        "backtest_reference": BACKTEST_REFERENCE,
        "lab": True,
        "min_warmup_bars": 50,
        "default_warmup_bars": 100,
        "upstream": "local",
    }


@app.get("/api/backtest-reference")
def backtest_reference():
    return {"ok": True, **BACKTEST_REFERENCE}


@app.get("/api/broker/state")
def broker_state():
    return _broker_handle("GET", "/api/broker/state")


@app.get("/api/broker/orders")
def broker_orders():
    return _broker_handle("GET", "/api/broker/orders")


@app.get("/api/broker/executions")
def broker_executions():
    return _broker_handle("GET", "/api/broker/executions")


@app.post("/api/broker/heartbeat")
def broker_heartbeat(body: dict = Body(default_factory=dict)):
    return _broker_handle("POST", "/api/broker/heartbeat", body)


@app.post("/api/broker/execution")
def broker_execution(body: dict = Body(default_factory=dict)):
    return _broker_handle("POST", "/api/broker/execution", body)


@app.post("/api/broker/reset")
def broker_reset(body: dict = Body(default_factory=dict)):
    return _broker_handle("POST", "/api/broker/reset", body)


@app.get("/api/broker/controls")
def broker_controls_get():
    return _broker_handle("GET", "/api/broker/controls")


@app.post("/api/broker/controls")
def broker_controls_post(body: dict = Body(default_factory=dict)):
    return _broker_handle("POST", "/api/broker/controls", body)


@app.options("/api/broker/{path:path}")
@app.options("/live")
@app.options("/")
def cors_preflight(path: str = ""):
    return JSONResponse(status_code=200, content={"ok": True})


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("gcp_main:app", host="0.0.0.0", port=port, reload=False)
