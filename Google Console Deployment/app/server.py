"""
FastAPI wrapper around the rules engine for the GCP desk.

The trading rules live in mt5_live_engine.py (one copy, shared with the Ali
PC bridge). This module only adds:
  * one process-wide engine guarded by an RLock
  * /health, /reset, /warmup, /signal (called in-process by lab_api.proxy)
  * atomic state persistence so a service restart keeps open paper tickets
  * the broker order hint the lab's paper accounts and webhooks consume
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import mt5_live_engine as eng
from mt5_live_engine import LatestModsEngine

API_KEY = os.environ.get("API_KEY", "")
SYMBOL = os.environ.get("SYMBOL", "XAUUSD")
MODEL_LABEL = os.environ.get("MODEL_NAME", "Asim GCP Live")
STATE_FILE = Path(os.environ.get("STATE_FILE", "/opt/asim-gcp/data/engine_state.json"))


class Candle(BaseModel):
    # Signal OHLC (Heikin-Ashi). raw_* is the executable broker candle; all
    # four raw fields must be sent together.
    open: float
    high: float
    low: float
    close: float
    raw_open: Optional[float] = None
    raw_high: Optional[float] = None
    raw_low: Optional[float] = None
    raw_close: Optional[float] = None
    xtrend: float = 0.0
    hist: float = 0.0
    histcolor: Optional[str] = None
    time: Optional[str] = None
    skip_worst_hours: Optional[bool] = None
    focus_best_hours: Optional[bool] = None


class WarmupReq(BaseModel):
    candles: List[Candle]


def _auth(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")


def _validate_raw_group(candles: "List[Candle]") -> None:
    """A partial raw_* group is a 400, not an unhandled 500."""
    for c in candles:
        vals = (c.raw_open, c.raw_high, c.raw_low, c.raw_close)
        if any(v is not None for v in vals) and not all(v is not None for v in vals):
            raise HTTPException(status_code=400, detail="raw_open, raw_high, raw_low and raw_close must be sent together")


engine = LatestModsEngine()
# /signal, /warmup and /reset all mutate one engine; uvicorn serves on a
# thread pool, so concurrent bars must not interleave inside push().
engine_lock = threading.RLock()


def _save_engine_state() -> None:
    """Best-effort atomic persistence; a read-only volume must never take /signal down."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(engine.snapshot(), separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _load_engine_state() -> bool:
    if not STATE_FILE.is_file():
        return False
    try:
        engine.restore(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        return True
    except Exception:
        engine.reset()
        return False


STATE_RESTORED = _load_engine_state()
app = FastAPI(title=f"XAUUSD {MODEL_LABEL}", version="8.0-single-engine")


def _order_hint(res: dict) -> Optional[dict]:
    """Broker instructions for one engine result (same shape the bridge uses)."""
    if res.get("duplicate_bar"):
        return None
    orders: list[dict] = []
    vol = float(res.get("fill_lot") or eng.effective_volume())
    active_sl = res.get("sl_updated", res.get("sl"))
    filled = res.get("filled_action") or (
        res["action"] if res.get("action") in ("BUY", "SELL", "BUY_ADD", "SELL_ADD") else None)
    if filled:
        entry_sl = res.get("fill_sl") if res.get("fill_sl") is not None else active_sl
        orders.append({"actionType": "ORDER_TYPE_BUY" if "BUY" in filled else "ORDER_TYPE_SELL",
                       "symbol": SYMBOL, "volume": vol, "sl": entry_sl, "stopLoss": entry_sl,
                       "kind": "PRIMARY" if filled in ("BUY", "SELL") else "SUPP"})
    if res.get("action") == "EXIT" or (res.get("sl_exits") and res.get("n_total", 1) == 0):
        orders.append({"actionType": "POSITIONS_CLOSE_SYMBOL", "symbol": SYMBOL})
    else:
        for closed in ([res["closed_primary"]] if res.get("closed_primary") else []) + list(res.get("closed_supps") or []):
            orders.append({"actionType": "POSITIONS_CLOSE_PARTIAL_SYMBOL", "symbol": SYMBOL,
                           "volume": float(closed.get("lot") or vol), "entry": closed.get("entry"),
                           "ticket": closed.get("ticket"), "exit": closed.get("exit"), "reason": closed.get("reason")})
    if res.get("sl_changed") and active_sl is not None and res.get("n_total", 0) > 0:
        orders.append({"actionType": "SL_MODIFY", "symbol": SYMBOL, "sl": active_sl, "stopLoss": active_sl,
                       "positions": res.get("open_positions") or []})
    if not orders:
        return None
    return orders[0] if len(orders) == 1 else {"orders": orders}


@app.get("/health")
def health():
    with engine_lock:
        return {
            "ok": True, "model": MODEL_LABEL, "mode": engine._mode_label(),
            "warmed_up": engine.warmed,
            "position": {0: "FLAT", 1: "LONG", -1: "SHORT"}[engine.pos],
            "n_units": engine._n_primary(), "n_supp": engine._n_supp(), "n_total": engine._n_total(),
            "sl": engine._active_sl(),
            "entry_every_candle": eng.effective_every_candle(),
            "hist_thresh": eng.effective_hist_thresh(),
            "volume": eng.effective_volume(), "maxpos": eng.effective_maxpos(), "max_supp": eng.effective_max_supp(),
            "tsl_atr_mult": eng.effective_tsl_atr_mult(), "atr": round(float(engine._atr), 4),
            "tsl_ticks": eng.effective_tsl_ticks(), "tsl_tick_size": eng.effective_tick_size(),
            "tsl_distance": round(engine._trade_tsl(), 4),
            "tsl_mode": "atr" if eng.effective_tsl_atr_mult() > 0 else "ticks",
            "xtrend_gate": eng.effective_xt_gate(), "xtrend_gate_supp": eng.effective_xt_gate_supp(),
            "xtrend_touch_buf": eng.effective_xt_buf(),
            "stop_slippage_pts": eng.effective_stop_slippage(),
            "entry_bar_mode": eng.effective_entry_bar_mode(),
            "trail_every_candle": eng.effective_trail_every_candle(),
            "trail_entry_bar": eng.effective_trail_entry_bar(),
            "last_execution_source": engine.last_execution_source,
            "raw_execution_active": engine.last_execution_source == "raw_ohlc",
            "spread_cost": eng.effective_spread_cost(), "balance": round(engine.balance, 4),
            "dedupe_cached_bars": len(engine._seen_bars),
            "state_persistence": str(STATE_FILE), "state_restored_on_boot": STATE_RESTORED,
            "xt_skips": engine.xt_skips, "skip_worst_hours": engine.skip_worst_hours,
            "focus_best_hours": engine.focus_best_hours,
        }


@app.post("/reset")
def reset(x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    with engine_lock:
        engine.reset()
        _save_engine_state()
    return {"ok": True}


def _push(c: Candle) -> dict:
    return engine.push(c.open, c.high, c.low, c.close, c.xtrend, c.hist, c.histcolor,
                       skip_worst_hours=c.skip_worst_hours, focus_best_hours=c.focus_best_hours,
                       time_str=c.time, raw_open=c.raw_open, raw_high=c.raw_high,
                       raw_low=c.raw_low, raw_close=c.raw_close)


@app.post("/warmup")
def warmup(req: WarmupReq, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    _validate_raw_group(req.candles)
    with engine_lock:
        engine.reset()
        last = None
        for c in req.candles:
            last = _push(c)
        # Replay only: indicator/ATR context is kept, trades and counters are not.
        engine.pos, engine.positions, engine.break_level, engine.run_side = 0, [], None, 0
        engine.last_trade_close = None
        engine.balance = eng.START_BALANCE
        engine.hour_skips = engine.xt_skips = 0
        engine._seen_bars.clear()
        _save_engine_state()
    return {"ok": True, "fed": len(req.candles), "warmed_up": engine.warmed, "position": "FLAT",
            "n_units": 0, "mode": engine._mode_label(), "last_action": None,
            "warmup_last_action": None if last is None else last.get("action"),
            "trade_state_cleared": True, "balance_reset": True, "balance": engine.balance}


@app.post("/signal")
def signal(c: Candle, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    _validate_raw_group([c])
    with engine_lock:
        res = _push(c)
        res["time"] = c.time
        res["order"] = _order_hint(res)
        res["send_order"] = res["order"] is not None
        if not res.get("duplicate_bar"):
            _save_engine_state()
    return res
