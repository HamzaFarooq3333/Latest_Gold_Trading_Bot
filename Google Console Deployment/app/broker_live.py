"""Persist and serve the Asim MT5 live desk snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
import os

MAX_ITEMS = 512
EXPECTED_LOGIN = int(os.environ.get("EXPECTED_MT5_LOGIN", "472640728"))
EXPECTED_SYMBOL = os.environ.get("EXPECTED_MT5_SYMBOL", "XAUUSDm")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _body(event: dict) -> dict:
    import json

    try:
        return json.loads(event.get("body") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _default_controls() -> dict:
    return {
        "VOLUME": 0.01,
        "TSL_PTS": 0.25,
        "TSL_PCT": 0.25,
        "TSL_TICKS": 1111,
        "TSL_TICK_SIZE": 0.001,
        "BROKER_MIN_STOP_PTS": 0.30,
        "STOP_SLIPPAGE_PTS": 0,
        "SPREAD_COST": 0.06,
        "TRAIL_EVERY_CANDLE": 1,
        "TRAIL_ENTRY_BAR": 1,
        "ENTRY_BAR_MODE": "defer",
        "DISABLE_STOP_LOSS": 0,
        "MAXPOS": 20,
        "MAX_SUPP": 10,
        "BEST_LOT_MULT": 2.0,
        "HIST_THRESH": 10,
        "XTREND_GATE": 1,
        "XTREND_GATE_SUPP": 0,
        "XTREND_SOURCE": "supertrend",
        "SKIP_WEEKENDS": 0,
        "updated_at": None,
        "updated_by": None,
    }


def _default() -> dict:
    return {
        "account": {},
        "positions": [],
        "orders": [],
        "trades": [],
        "trade_history": [],
        "bars": [],
        "bar_history": [],
        "heartbeats": [],
        "equity_trail": [],
        "sl_trail": [],
        "ohlc_records": [],
        "execution_log": [],
        "analysis": {},
        "bridge": {},
        "controls": _default_controls(),
        "symbol": None,
        "values_source": "PYTHON_BRIDGE",
        "values_updated_at": None,
        "updated_at": None,
        "last_heartbeat_at": None,
        "heartbeat_age_sec": None,
        "session_started_at": None,
        "mt5_connection": None,
    }


def _trim(value, limit: int = MAX_ITEMS) -> list:
    return list(value or [])[-limit:]


def _age(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except (TypeError, ValueError):
        return None


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _after_session(items: list, cutoff: datetime | None, keys: tuple[str, ...] = ("time", "open_time", "close_time", "ts")) -> list:
    if cutoff is None:
        return list(items or [])
    out = []
    for item in items or []:
        stamp = None
        for key in keys:
            stamp = _parse_ts(item.get(key))
            if stamp is not None:
                break
        # Keep forming bars even if their open is slightly before the wipe clock.
        if stamp is None or stamp >= cutoff or bool(item.get("forming")):
            out.append(item)
    return out


def _session_bar_cutoff(cutoff: datetime | None) -> datetime | None:
    """Floor session start to the M15 open so the live chart keeps the current candle."""
    if cutoff is None:
        return None
    minute = (cutoff.minute // 15) * 15
    return cutoff.replace(minute=minute, second=0, microsecond=0)


def _fill_bar_time(open_time) -> str | None:
    """M15 open (UTC) of the candle where the fill occurred — chart arrow anchor."""
    stamp = _parse_ts(open_time)
    if stamp is None:
        return None
    minute = (stamp.minute // 15) * 15
    bar = stamp.replace(minute=minute, second=0, microsecond=0)
    return bar.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_trade_bar_times(trades: list) -> list:
    """Guarantee every trade has bar_time = fill candle so UI arrows land correctly."""
    out = []
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        row = dict(t)
        if not row.get("bar_time"):
            bt = _fill_bar_time(row.get("open_time") or row.get("time"))
            if bt:
                row["bar_time"] = bt
        out.append(row)
    return out


def _session_analysis(trades: list) -> dict:
    closed = [
        t for t in (trades or [])
        if str(t.get("status") or "") == "closed" or t.get("close_time")
    ]
    if not closed:
        return {
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_pct": 0,
            "net_profit": 0,
            "gross_profit": 0,
            "gross_loss": 0,
            "profit_factor": None,
            "largest_win": 0,
            "largest_loss": 0,
            "primary_count": 0,
            "primary_pnl": 0,
            "supp_count": 0,
            "supp_pnl": 0,
        }
    pnls = [float(t.get("profit") or 0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    prim = [t for t in closed if str(t.get("kind") or "").upper() == "PRIMARY"]
    supp = [t for t in closed if str(t.get("kind") or "").upper() == "SUPP"]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_pct": round(100.0 * len(wins) / len(closed), 2) if closed else 0,
        "net_profit": round(sum(pnls), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "largest_win": round(max(pnls), 2),
        "largest_loss": round(min(pnls), 2),
        "primary_count": len(prim),
        "primary_pnl": round(sum(float(t.get("profit") or 0) for t in prim), 2),
        "supp_count": len(supp),
        "supp_pnl": round(sum(float(t.get("profit") or 0) for t in supp), 2),
    }


def handle(method: str, path: str, event: dict, *, lab, model_name: str, response):
    """Return a Lambda response for Asim broker-live routes, or None."""
    if not path.startswith("/api/broker/"):
        return None

    store = lab.load_store(model_name)
    broker = dict(store.get("broker_live") or _default())
    broker.setdefault("values_source", "PYTHON_BRIDGE")
    broker.setdefault("values_updated_at", None)

    if method == "POST" and path == "/api/broker/heartbeat":
        body = _body(event)
        now = _now()
        incoming_source = body.get("values_source") or (body.get("bridge") or {}).get("values_source")
        incoming_mt5_values = incoming_source == "MT5_TERMINAL"
        if incoming_mt5_values:
            broker["values_source"] = "MT5_TERMINAL"
            broker["values_updated_at"] = now
        for key in (
            "account",
            "positions",
            "orders",
            "trades",
            "bars",
            "analysis",
            "bridge",
            "symbol",
            "sl_trail",
            "ohlc_records",
            "xtrend_source",
            "tick_bid",
            "tick_ask",
            "tick_time",
            "feed_updated_at",
            "values_updated_at",
        ):
            if key in body:
                # Once the MT5 publisher is active, the Python bridge may still
                # update account/trade state, but it must not replace the
                # publisher's exact indicator buffers with recalculated values.
                if key == "bars" and not incoming_mt5_values and broker.get("values_source") == "MT5_TERMINAL":
                    continue
                if key == "bridge":
                    merged_bridge = dict(broker.get("bridge") or {})
                    merged_bridge.update(body.get("bridge") or {})
                    broker[key] = merged_bridge
                else:
                    broker[key] = body[key]
        broker["positions"] = _trim(broker.get("positions"))
        cutoff = _parse_ts(broker.get("session_started_at"))
        bar_cutoff = _session_bar_cutoff(cutoff)
        # Trades/orders use the exact wipe clock so a reset truly zeros P/L and
        # old fills on the current M15 bar cannot reappear via bridge history.
        broker["orders"] = [
            o for o in _after_session(_trim(broker.get("orders")), cutoff)
            if str(o.get("kind") or "").upper() in ("PRIMARY", "SUPP")
        ]
        broker["trades"] = [
            t for t in _after_session(_trim(broker.get("trades")), cutoff, ("open_time", "close_time", "time"))
            if str(t.get("kind") or "").upper() in ("PRIMARY", "SUPP")
        ]
        # Keep trade_history mirrored so UIs that prefer that key still see arrows/rows.
        # An empty trade_history list must never hide live trades.
        hist = [
            t for t in _after_session(_trim(broker.get("trade_history")), cutoff, ("open_time", "close_time", "time"))
            if str(t.get("kind") or "").upper() in ("PRIMARY", "SUPP")
        ]
        broker["trade_history"] = hist if len(hist) >= len(broker["trades"]) else list(broker["trades"])
        # Prefer the fuller list so TradingView arrows cover every recorded fill.
        if broker["trades"] and broker["trade_history"]:
            by_key = {}
            for t in list(broker["trade_history"]) + list(broker["trades"]):
                key = str(
                    t.get("ticket")
                    or t.get("position_id")
                    or f"{t.get('open_time')}|{t.get('side')}|{t.get('open_price')}|{t.get('kind')}"
                )
                by_key[key] = t
            merged = list(by_key.values())
            broker["trades"] = merged
            broker["trade_history"] = merged
        # Drop pre-session history the bridge re-pushes (keep current M15 open onward).
        broker["bars"] = _trim(_after_session(broker.get("bars"), bar_cutoff), 240)
        broker["ohlc_records"] = _trim(
            _after_session(broker.get("ohlc_records"), bar_cutoff), 500
        )
        # Keep bar_history in sync so older UIs that only read bar_history still work.
        if broker.get("bars"):
            broker["bar_history"] = list(broker.get("bars") or [])
        elif broker.get("ohlc_records"):
            # Synthesize chart bars from OHLC audit rows if strategy bars were dropped.
            synthesized = []
            for row in broker.get("ohlc_records") or []:
                synthesized.append(
                    {
                        "time": row.get("time"),
                        "open": row.get("ha_open"),
                        "high": row.get("ha_high"),
                        "low": row.get("ha_low"),
                        "close": row.get("ha_close"),
                        "raw_open": row.get("raw_open"),
                        "raw_high": row.get("raw_high"),
                        "raw_low": row.get("raw_low"),
                        "raw_close": row.get("raw_close"),
                        "hist": row.get("hist"),
                        "xtrend": row.get("xtrend"),
                        "histcolor": row.get("zone") or row.get("histcolor"),
                        "zone": row.get("zone") or row.get("histcolor"),
                        "action": row.get("action") or "NONE",
                        "forming": row.get("forming"),
                    }
                )
            broker["bars"] = _trim(synthesized, 240)
            broker["bar_history"] = list(broker["bars"])
        else:
            broker["bar_history"] = []
        broker["heartbeats"] = _trim(broker.get("heartbeats"))
        broker["equity_trail"] = _after_session(_trim(broker.get("equity_trail"), 3600), cutoff, ("time", "ts"))
        broker["sl_trail"] = _after_session(_trim(broker.get("sl_trail"), 2000), cutoff)
        broker["execution_log"] = _after_session(
            _trim(broker.get("execution_log"), 200), cutoff, ("time", "ts", "bar_time")
        )
        broker["analysis"] = _session_analysis(broker.get("trades") or [])
        broker.setdefault("controls", _default_controls())
        account = broker.get("account") or {}
        equity = account.get("equity")
        balance = account.get("balance")
        profit = account.get("profit")
        if equity is not None:
            broker["equity_trail"].append(
                {
                    "time": now,
                    "ts": now,
                    "equity": equity,
                    "balance": balance,
                    "profit": profit,
                }
            )
            broker["equity_trail"] = broker["equity_trail"][-3600:]
        broker["heartbeats"].append(
            {
                "time": now,
                "ts": now,
                "host": (broker.get("bridge") or {}).get("host"),
                "lab_action": body.get("lab_action") or (broker.get("bridge") or {}).get("engine_action"),
                "bar_time": body.get("bar_time") or ((broker.get("bars") or [{}])[-1].get("time") if broker.get("bars") else None),
                "note": body.get("note") or f"bid={broker.get('tick_bid')} ask={broker.get('tick_ask')}",
                "login": account.get("login"),
                "symbol": broker.get("symbol"),
                "equity": equity,
                "positions": len(broker.get("positions") or []),
                "mt5_connection": (broker.get("bridge") or {}).get(
                    "mt5_connection", "ONLINE"
                ),
                "tick_bid": broker.get("tick_bid"),
                "tick_ask": broker.get("tick_ask"),
            }
        )
        if body.get("values_updated_at") or body.get("feed_updated_at"):
            broker["values_updated_at"] = body.get("values_updated_at") or body.get("feed_updated_at")
        elif broker.get("values_source") != "MT5_TERMINAL":
            broker["values_updated_at"] = now
            broker["values_source"] = broker.get("values_source") or "PYTHON_BRIDGE"
        broker["heartbeats"] = broker["heartbeats"][-MAX_ITEMS:]
        broker["last_heartbeat_at"] = now
        broker["updated_at"] = now
        broker["heartbeat_age_sec"] = 0
        broker["mt5_connection"] = (broker.get("bridge") or {}).get("mt5_connection", "ONLINE")
        store["broker_live"] = broker
        lab.save_store(model_name, store)
        return response(
            200,
            {
                "ok": True,
                "saved_at": now,
                "expected_login": EXPECTED_LOGIN,
                "expected_symbol": EXPECTED_SYMBOL,
                "positions": len(broker["positions"]),
                "trades": len(broker["trades"]),
            },
        )

    if method == "POST" and path == "/api/broker/execution":
        body = _body(event)
        now = _now()
        results = list(body.get("results") or [])
        bridge = body.get("bridge") or {}
        for key in ("account", "positions", "symbol", "bridge"):
            if key in body:
                if key == "bridge":
                    merged_bridge = dict(broker.get("bridge") or {})
                    merged_bridge.update(body.get("bridge") or {})
                    broker[key] = merged_bridge
                else:
                    broker[key] = body[key]
        broker["positions"] = _trim(broker.get("positions"))
        broker["execution_log"] = _trim(broker.get("execution_log"))
        entry = {
            "id": f"{now}:{body.get('bar_time') or 'none'}",
            "time": now,
            "bar_time": body.get("bar_time"),
            "action": body.get("action"),
            "ok": bool(body.get("ok")),
            "results": results,
            "note": str(body.get("note") or ""),
            "had_broker_orders": bool(body.get("had_broker_orders")),
            "symbol": body.get("symbol") or broker.get("symbol"),
        }
        broker["execution_log"].append(entry)
        broker["execution_log"] = broker["execution_log"][-MAX_ITEMS:]
        broker["last_execution_at"] = now
        broker["updated_at"] = now
        broker["mt5_connection"] = bridge.get(
            "mt5_connection", broker.get("mt5_connection", "ONLINE")
        )
        store["broker_live"] = broker
        lab.save_store(model_name, store)
        return response(
            200,
            {
                "ok": True,
                "saved_at": now,
                "matched": bool(body.get("bar_time")),
                "mt5_status": broker["mt5_connection"],
                "execution_id": entry["id"],
            },
        )

    if method == "GET" and path == "/api/broker/state":
        broker["heartbeat_age_sec"] = _age(broker.get("last_heartbeat_at"))
        broker["values_age_sec"] = _age(broker.get("values_updated_at"))
        # Re-apply exact session wipe on read so stale bridge history cannot linger in the UI.
        cutoff = _parse_ts(broker.get("session_started_at"))
        if cutoff is not None:
            broker["trades"] = [
                t
                for t in _after_session(
                    broker.get("trades") or [], cutoff, ("open_time", "close_time", "time")
                )
                if str(t.get("kind") or "").upper() in ("PRIMARY", "SUPP")
            ]
            broker["trade_history"] = [
                t
                for t in _after_session(
                    broker.get("trade_history") or [], cutoff, ("open_time", "close_time", "time")
                )
                if str(t.get("kind") or "").upper() in ("PRIMARY", "SUPP")
            ]
            if not broker["trade_history"] and broker["trades"]:
                broker["trade_history"] = list(broker["trades"])
            broker["orders"] = [
                o
                for o in _after_session(broker.get("orders") or [], cutoff)
                if str(o.get("kind") or "").upper() in ("PRIMARY", "SUPP")
            ]
            broker["execution_log"] = _after_session(
                broker.get("execution_log") or [], cutoff, ("time", "ts", "bar_time")
            )
            broker["analysis"] = _session_analysis(broker.get("trades") or [])
        # Never expose an empty trade_history when trades are present (breaks chart arrows).
        if not (broker.get("trade_history") or []) and (broker.get("trades") or []):
            broker["trade_history"] = list(broker.get("trades") or [])
        broker["trades"] = _ensure_trade_bar_times(broker.get("trades") or [])
        broker["trade_history"] = _ensure_trade_bar_times(broker.get("trade_history") or [])
        return response(
            200,
            {
                "ok": True,
                "broker": broker,
                "expected_login": EXPECTED_LOGIN,
                "expected_symbol": EXPECTED_SYMBOL,
                "model_name": model_name,
            },
        )

    if method == "GET" and path == "/api/broker/orders":
        return response(200, {"ok": True, "orders": broker.get("orders") or []})

    if method == "GET" and path == "/api/broker/executions":
        return response(200, {"ok": True, "execution_log": broker.get("execution_log") or []})

    if method == "POST" and path == "/api/broker/reset":
        # Keep live controls across reset so operators can retune without re-entry.
        prev_controls = dict((broker.get("controls") or _default_controls()))
        now = _now()
        store["broker_live"] = _default()
        store["broker_live"]["controls"] = prev_controls
        store["broker_live"]["session_started_at"] = now
        store["broker_live"]["updated_at"] = now
        store["broker_live"]["note"] = "fresh start — waiting for new MT5 heartbeats"
        lab.save_store(model_name, store)
        return response(200, {"ok": True, "reset": True, "session_started_at": now})

    if method == "GET" and path == "/api/broker/controls":
        controls = dict(_default_controls())
        controls.update(broker.get("controls") or {})
        return response(200, {"ok": True, "controls": controls})

    if method == "POST" and path == "/api/broker/controls":
        body = _body(event)
        controls = dict(_default_controls())
        controls.update(broker.get("controls") or {})
        allowed = {
            "VOLUME",
            "TSL_PTS",
            "TSL_PCT",
            "TSL_TICKS",
            "TSL_TICK_SIZE",
            "BROKER_MIN_STOP_PTS",
            "STOP_SLIPPAGE_PTS",
            "SPREAD_COST",
            "TRAIL_EVERY_CANDLE",
            "TRAIL_ENTRY_BAR",
            "ENTRY_BAR_MODE",
            "DISABLE_STOP_LOSS",
            "MAXPOS",
            "MAX_SUPP",
            "BEST_LOT_MULT",
            "HIST_THRESH",
            "XTREND_GATE",
            "XTREND_GATE_SUPP",
            "XTREND_SOURCE",
            "SKIP_WEEKENDS",
        }
        incoming = body.get("controls") if isinstance(body.get("controls"), dict) else body
        for key, value in (incoming or {}).items():
            if key in allowed:
                controls[key] = value
        controls["updated_at"] = _now()
        controls["updated_by"] = body.get("updated_by") or "dashboard"
        broker["controls"] = controls
        store["broker_live"] = broker
        lab.save_store(model_name, store)
        return response(200, {"ok": True, "controls": controls})

    return response(404, {"error": "not found", "path": path})
