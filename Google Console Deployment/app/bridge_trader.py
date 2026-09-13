"""
Onyxion MT5 bridge - runs the rules engine on closed MT5 M15 candles and
executes on the Exness demo account.

    python bridge_trader.py --model ASIM          # loop (the watchdog runs this)
    python bridge_trader.py --model ASIM --once   # one sync then exit

Per poll (MT5_POLL_SECONDS, default 2 s):
  1. pull the desk's live controls (only applied once someone saved them)
  2. reconcile the engine's tickets with what the broker actually holds
  3. feed every newly closed M15 bar to mt5_live_engine.LatestModsEngine
  4. translate the engine result into MT5 orders (entry / stop modify /
     partial close / flatten) and retry a failed leg safely
  5. POST a full snapshot (account, positions, trades, bars, decisions) to the
     GCP live desk and write state/bridge_status.json for the GitHub agent

Demo only. Credentials come from .env next to this file; never log them.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import socket
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

import mt5_live_engine
from mt5_live_engine import Mt5LiveEngine

HERE = Path(__file__).resolve().parent
# Install root is flat (C:\onyxion-ali\bridge_trader.py next to .env); in the
# repo the file lives under runtime/ and .env is not present.
ENV_PATH = HERE / ".env" if (HERE / ".env").is_file() else HERE.parent / ".env"
_DATA_ROOT = HERE if (HERE / ".env").is_file() else HERE.parent
STATE_DIR = _DATA_ROOT / "state"
LOG_DIR = _DATA_ROOT / "logs"
STATUS_FILE_NAME = "bridge_status.json"

# A pending bar's orders are retried, but never forever: the engine must reach
# newer bars to keep trailing stops and the amber flatten alive.
MAX_PENDING_ATTEMPTS = int(os.environ.get("MAX_PENDING_ATTEMPTS", "8"))
# Entries from candles that closed more than this long ago are not sent: a
# catch-up replay must not fill an old signal at the current market price.
STALE_ENTRY_SECONDS = float(os.environ.get("STALE_ENTRY_SECONDS", "1800"))
# Closed-bar window the indicators are seeded from (matches the MQ5 indicators).
MT5_CALC_WINDOW = 160
MT5_XTREND_PERIOD = 6
MT5_XTREND_MULT = 0.8
MAX_LOCAL_BAR_RECORDS = 20000
LOG_RETENTION_DAYS = 14
# Partial-close fallback: a broker ticket within this many price units of the
# engine's entry is treated as the same trade when no ticket id is bound.
ENTRY_MATCH_TOLERANCE = 1.0

_EXECUTOR_LOCK_HANDLE = None
_LAST_PRUNE_DAY = ""
_LAST_SYMBOL_LOGGED = ""


# --------------------------------------------------------------------------- #
# Process lock, env, logging                                                   #
# --------------------------------------------------------------------------- #

def _executor_lock_file(model: str) -> Path:
    return STATE_DIR / f"{str(model or 'ASIM').strip().lower()}_mt5_executor.lock"


def acquire_local_executor(model: str) -> None:
    """Exactly one bridge process may submit orders for this model."""
    global _EXECUTOR_LOCK_HANDLE
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(_executor_lock_file(model), "a+", encoding="utf-8")
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write("0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit(f"REFUSED: another {str(model).upper()} MT5-bar executor is already running")
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"pid": os.getpid(), "model": str(model).upper(), "mode": "MT5 BARS"}))
    handle.flush()
    _EXECUTOR_LOCK_HANDLE = handle


def release_local_executor() -> None:
    global _EXECUTOR_LOCK_HANDLE
    if _EXECUTOR_LOCK_HANDLE is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            _EXECUTOR_LOCK_HANDLE.seek(0)
            msvcrt.locking(_EXECUTOR_LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_EXECUTOR_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    finally:
        _EXECUTOR_LOCK_HANDLE.close()
        _EXECUTOR_LOCK_HANDLE = None


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"Missing {path} - put .env next to bridge_trader.py")
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def apply_engine_environment(env: dict[str, str]) -> None:
    """Export .env then reload the engine so its import-time constants
    (LEVERAGE, START_BALANCE, MARGIN_MIN_PCT) see the file's values."""
    global Mt5LiveEngine
    os.environ.update(env)
    module = importlib.reload(mt5_live_engine)
    Mt5LiveEngine = module.Mt5LiveEngine


def log(msg: str) -> None:
    global _LAST_PRUNE_DAY
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    line = f"[{now:%Y-%m-%d %H:%M:%S} UTC] {msg}"
    print(line, flush=True)
    day = now.strftime("%Y%m%d")
    with open(LOG_DIR / f"bridge_{day}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if day != _LAST_PRUNE_DAY:
        _LAST_PRUNE_DAY = day
        _prune_old_logs(now)


def _prune_old_logs(now: datetime) -> None:
    """Daily bridge logs older than LOG_RETENTION_DAYS are deleted (they were
    growing without bound - 40 MB after two weeks)."""
    cutoff = (now - timedelta(days=LOG_RETENTION_DAYS)).strftime("%Y%m%d")
    for f in LOG_DIR.glob("bridge_????????.log"):
        if f.stem[7:] < cutoff:
            try:
                f.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Configuration + desk controls                                                #
# --------------------------------------------------------------------------- #

def build_cfg(env: dict) -> dict:
    lab_raw = (env.get("ASIM_LAB_URL") or "").strip()
    if not lab_raw:
        raise SystemExit("ASIM_LAB_URL is required in .env (the GCP live desk, e.g. https://35.253.21.246)")
    return {
        "model": "ASIM",
        "login": int(env["ASIM_MT5_LOGIN"]),
        "password": env["ASIM_MT5_PASSWORD"],
        "server": env["ASIM_MT5_SERVER"],
        "magic": int(env.get("MT5_MAGIC_ASIM", "126823")),
        "lab_url": lab_raw.rstrip("/") + "/",
        "lot": float(env.get("VOLUME", env.get("ASIM_MT5_LOT", "0.01"))),
        "symbol": env.get("ASIM_MT5_SYMBOL", env.get("MT5_SYMBOL", "XAUUSDm")),
        "deviation": int(env.get("MT5_DEVIATION", "30")),
        "poll": int(env.get("MT5_POLL_SECONDS", "2")),
        "terminal": env.get("ASIM_MT5_TERMINAL_PATH", env.get("MT5_TERMINAL_PATH", "")),
        "portable": env.get("ASIM_MT5_PORTABLE", env.get("MT5_PORTABLE", "1")).strip().lower() in ("1", "true", "yes", "on"),
        "execution_source": "mt5_bars",
        # Broker-side minimum stop distance (price units). The engine's stop is
        # never widened; only the value submitted to MT5 is clamped.
        "broker_min_stop_pts": float(env.get("BROKER_MIN_STOP_PTS", "0.30")),
    }


# Desk controls the bridge honours. Anything else the desk sends is ignored.
LIVE_CONTROL_KEYS = (
    "VOLUME", "HIST_THRESH", "ENTRY_EVERY_CANDLE", "TSL_ATR_MULT",
    "TSL_TICKS", "TSL_TICK_SIZE", "TSL_PTS", "BROKER_MIN_STOP_PTS",
    "STOP_SLIPPAGE_PTS", "SPREAD_COST", "TRAIL_EVERY_CANDLE", "TRAIL_ENTRY_BAR",
    "ENTRY_BAR_MODE", "DISABLE_STOP_LOSS", "MAXPOS", "MAX_SUPP", "BEST_LOT_MULT",
    "XTREND_GATE", "XTREND_GATE_SUPP", "XTREND_SOURCE", "SKIP_WEEKENDS",
)


def apply_live_controls(cfg: dict) -> None:
    """Pull the dashboard's live controls and export them to the engine env.

    .env is the source of truth. The desk's stored controls are applied ONLY
    when somebody has actually saved them on the dashboard (updated_at set).
    A desk reset or redeploy hands back untouched defaults with no updated_at,
    and those must never overwrite the config on this PC - that is exactly
    what kept resetting VOLUME / HIST_THRESH / MAX_SUPP before.
    """
    try:
        data = http_json(cfg["lab_url"].rstrip("/") + "/api/broker/controls", timeout=8)
        controls = (data or {}).get("controls") or {}
    except Exception as exc:
        log(f"live controls unavailable: {exc}")
        return
    if not controls.get("updated_at"):
        if cfg.get("_live_controls_source") != "env":
            log("desk controls not saved by anyone - .env governs")
            cfg["_live_controls_source"] = "env"
            cfg["_live_controls"] = {}
        return
    prev = cfg.get("_live_controls") or {}
    changed = []
    applied = {}
    for key in LIVE_CONTROL_KEYS:
        if key not in controls or controls[key] is None:
            continue
        raw = str(controls[key])
        os.environ[key] = raw
        applied[key] = raw
        if key == "VOLUME":
            try:
                cfg["lot"] = float(raw)
            except ValueError:
                pass
        if str(prev.get(key)) != raw:
            changed.append(f"{key}={raw}")
    cfg["_live_controls"] = applied
    cfg["_live_controls_source"] = "desk"
    if changed:
        log("live controls applied (saved on desk " + str(controls.get("updated_at")) + "): " + ", ".join(changed))


# --------------------------------------------------------------------------- #
# HTTP to the desk                                                             #
# --------------------------------------------------------------------------- #

def _lab_ssl_context():
    """GCP desks use a self-signed certificate; set ASIM_LAB_INSECURE=0 to verify."""
    ctx = ssl.create_default_context()
    if os.environ.get("ASIM_LAB_INSECURE", "1").strip().lower() not in ("0", "false", "no"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_json(url: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_lab_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_lab_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# Indicators (must match mq5/OnyxionHistogram.mq5 and OnyxionXTrendProxy.mq5)  #
# --------------------------------------------------------------------------- #

def _wilder_rsi(closes: list[float], period: int = 3) -> list[float]:
    n = len(closes)
    rsi = [50.0] * n
    avg_g = avg_l = 0.0
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        if i == 1:
            avg_g, avg_l = g, l
        else:
            avg_g += (g - avg_g) / period
            avg_l += (l - avg_l) / period
        rsi[i] = 100.0 if avg_l <= 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return rsi


def _ema_span(src: list[float], span: int = 5) -> list[float]:
    if not src:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [src[0]]
    for x in src[1:]:
        out.append(alpha * x + (1.0 - alpha) * out[-1])
    return out


def hist_color(h: float, thresh: float | None = None) -> str:
    """Colour a histogram value with the LIVE threshold.

    The threshold is read at call time: a hard-coded 10 here once coloured
    -13 bars 'red' while .env said 15, so the engine traded a threshold the
    operator had never chosen.
    """
    th = mt5_live_engine.effective_hist_thresh() if thresh is None else float(thresh)
    if h >= th:
        return "green"
    if h <= -th:
        return "red"
    return "amber"


def trade_kind(comment: str) -> str:
    c = (comment or "").upper()
    if "PRIMARY" in c:
        return "PRIMARY"
    if "SUPP" in c or "ADD" in c:
        return "SUPP"
    return "OTHER"


def _heiken_ashi(o, h, l, c):
    n = len(c)
    ha_o, ha_c, ha_h, ha_l = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n
    for i in range(n):
        ha_c[i] = (o[i] + h[i] + l[i] + c[i]) / 4.0
        ha_o[i] = (o[i] + c[i]) / 2.0 if i == 0 else (ha_o[i - 1] + ha_c[i - 1]) / 2.0
        ha_h[i] = max(h[i], ha_o[i], ha_c[i])
        ha_l[i] = min(l[i], ha_o[i], ha_c[i])
    return ha_o, ha_c, ha_h, ha_l


def _wilder_atr(h, c, l, period):
    n = len(c)
    atr = [0.0] * n
    alpha = 1.0 / period
    total = 0.0
    for i in range(n):
        tr = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        if i < period:
            total += tr
            atr[i] = total / period if i == period - 1 else tr
        else:
            atr[i] = atr[i - 1] * (1.0 - alpha) + tr * alpha
    return atr


def _supertrend_ha(o, h, l, c, period=6, mult=0.8):
    """SuperTrend on Heikin-Ashi (Hamza's desk: XTREND_SOURCE=supertrend)."""
    _, hac, hah, hal = _heiken_ashi(o, h, l, c)
    atr = _wilder_atr(hah, hac, hal, period)
    st = [0.0] * len(c)
    fu = fl = 0.0
    d = 1
    for i in range(len(c)):
        u, dd = hac[i] - mult * atr[i], hac[i] + mult * atr[i]
        if i == 0:
            fu, fl, st[i] = u, dd, u
            continue
        fu = max(u, fu) if hac[i - 1] > fu else u
        fl = min(dd, fl) if hac[i - 1] < fl else dd
        if d == 1 and hac[i] < fu:
            d = -1
        elif d == -1 and hac[i] > fl:
            d = 1
        st[i] = fu if d == 1 else fl
    return st


def _sma_at(arr, i, length):
    return float("nan") if i + 1 < length else sum(arr[i - length + 1:i + 1]) / float(length)


def _extreme_offset(arr, i, length, highest: bool):
    start = max(0, i - length + 1)
    best, best_v = 0, float("-inf") if highest else float("inf")
    for off in range(0, i - start + 1):
        v = arr[i - off]
        if (v > best_v) if highest else (v < best_v):
            best_v, best = v, off
    return best


def _gaga_trend(h, l, c):
    """KJ GagaTrend (the TradingView X-Trend) on the given H/L/C series."""
    n = len(c)
    atr = _wilder_atr(h, c, l, 14)
    out = [0.0] * n
    trend = next_trend = prev_trend = 0
    max_low_price, min_high_price = float(l[0]), float(h[0])
    up = down = float("nan")
    for i in range(n):
        high_price = float(h[i - _extreme_offset(h, i, 2, True)])
        low_price = float(l[i - _extreme_offset(l, i, 3, False)])
        high_ma, low_fast = _sma_at(h, i, 2), _sma_at(l, i, 2)
        low_main, low_slow = _sma_at(l, i, 3), _sma_at(l, i, 4)
        prev_low = float(l[i - 1]) if i else float(l[i])
        prev_high = float(h[i - 1]) if i else float(h[i])
        if next_trend == 1:
            max_low_price = max(low_price, max_low_price)
            if high_ma == high_ma and high_ma < max_low_price and c[i] < prev_low:
                trend, next_trend, min_high_price = 1, 0, high_price
        else:
            min_high_price = min(high_price, min_high_price)
            bullish_main = (low_main == low_main and low_main > min_high_price) or \
                           (low_slow == low_slow and low_slow > min_high_price)
            bullish_fast = low_fast == low_fast and low_fast > min_high_price
            main_almost = atr[i] == atr[i] and low_main == low_main and \
                          low_main >= (min_high_price - atr[i] * 0.025)
            if (bullish_main or (bullish_fast and main_almost)) and c[i] > prev_high:
                trend, next_trend, max_low_price = 0, 1, low_price
        if trend == 0:
            up = (down if down == down else max_low_price) if prev_trend != 0 \
                else max(max_low_price, up if up == up else max_low_price)
        else:
            down = (up if up == up else min_high_price) if prev_trend != 1 \
                else min(min_high_price, down if down == down else min_high_price)
        out[i] = up if trend == 0 else down
        prev_trend = trend
    return out


def _compute_xtrend(opens, highs, lows, closes, ha_h, ha_l, ha_c) -> tuple[list[float], str]:
    """X-Trend series + label. XTREND_SOURCE is read live: gaga (Ali) or supertrend (Hamza)."""
    source = os.environ.get("XTREND_SOURCE", "supertrend").strip().lower()
    if source in ("gaga", "kj", "gagatrend", "vintage"):
        return _gaga_trend(ha_h, ha_l, ha_c), "gaga_ha"
    return _supertrend_ha(opens, highs, lows, closes, MT5_XTREND_PERIOD, MT5_XTREND_MULT), "supertrend_ha_6_0.8"


def collect_bars(symbol: str, count: int = 96) -> list[dict]:
    """Last `count` M15 bars (HA signal + raw + hist + X-Trend), forming bar last.

    Indicators are always seeded from MT5_CALC_WINDOW closed bars regardless
    of how many rows are returned, matching the MQ5 indicators.
    """
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, MT5_CALC_WINDOW + 1)
    if rates is None or len(rates) < 10:
        return []
    opens = [float(r["open"]) for r in rates]
    highs = [float(r["high"]) for r in rates]
    lows = [float(r["low"]) for r in rates]
    closes = [float(r["close"]) for r in rates]
    ha_o, ha_c, ha_h, ha_l = _heiken_ashi(opens, highs, lows, closes)
    ema = _ema_span(_wilder_rsi(closes, 3), 5)
    xt, xt_source = _compute_xtrend(opens, highs, lows, closes, ha_h, ha_l, ha_c)
    out = []
    for i in range(max(0, len(rates) - count), len(rates)):
        h = float(ema[i] - 50.0)
        t_unix = int(rates[i]["time"])            # bar open, broker clock = UTC
        t_iso = datetime.fromtimestamp(t_unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({
            "time": t_iso, "time_unix": t_unix, "time_utc": t_iso, "time_tz": "UTC",
            "open": ha_o[i], "high": ha_h[i], "low": ha_l[i], "close": ha_c[i], "candle_type": "HA",
            "raw_open": opens[i], "raw_high": highs[i], "raw_low": lows[i], "raw_close": closes[i],
            "signal_open": ha_o[i], "signal_high": ha_h[i], "signal_low": ha_l[i], "signal_close": ha_c[i],
            "forming": i == len(rates) - 1,
            "hist": round(h, 2), "histcolor": hist_color(h),
            "xtrend": round(float(xt[i]), 3), "xtrend_source": xt_source,
        })
    # Paint the forming candle from the live tick so /live moves between closes.
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and out:
            bid, ask = float(getattr(tick, "bid", 0.0) or 0.0), float(getattr(tick, "ask", 0.0) or 0.0)
            tick_unix = int(getattr(tick, "time", 0) or 0)
            last = out[-1]
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                last["tick_bid"], last["tick_ask"] = round(bid, 3), round(ask, 3)
                last["tick_time"] = datetime.fromtimestamp(tick_unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") \
                    if tick_unix else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                last["tick_time_unix"] = tick_unix or None
                if last.get("forming"):
                    last["raw_high"] = max(float(last["raw_high"]), ask, bid)
                    last["raw_low"] = min(float(last["raw_low"]), ask, bid)
                    last["raw_close"] = mid
                    hao = float(last["open"])
                    hac = (hao + float(last["raw_high"]) + float(last["raw_low"]) + mid) / 4.0
                    last["close"] = last["signal_close"] = hac
                    last["high"] = last["signal_high"] = max(float(last["raw_high"]), hao, hac)
                    last["low"] = last["signal_low"] = min(float(last["raw_low"]), hao, hac)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# Desk snapshot: trades, annotated bars                                        #
# --------------------------------------------------------------------------- #

def analyze_trades(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("status") == "closed"]
    wins = [t for t in closed if float(t.get("profit") or 0) > 0]
    losses = [t for t in closed if float(t.get("profit") or 0) <= 0]
    gp = sum(float(t["profit"]) for t in wins)
    gl = abs(sum(float(t["profit"]) for t in losses))
    prim = [t for t in closed if t.get("kind") == "PRIMARY"]
    supp = [t for t in closed if t.get("kind") == "SUPP"]
    n = len(closed)
    return {
        "closed_trades": n, "wins": len(wins), "losses": len(losses),
        "win_pct": round(100.0 * len(wins) / n, 2) if n else 0.0,
        "net_profit": round(sum(float(t.get("profit") or 0) for t in closed), 2),
        "gross_profit": round(gp, 2), "gross_loss": round(gl, 2),
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "largest_win": round(max((float(t["profit"]) for t in wins), default=0), 2),
        "largest_loss": round(min((float(t["profit"]) for t in losses), default=0), 2),
        "primary_count": len(prim), "primary_pnl": round(sum(float(t["profit"]) for t in prim), 2),
        "supp_count": len(supp), "supp_pnl": round(sum(float(t["profit"]) for t in supp), 2),
    }


def enrich_deal_kinds(orders: list[dict]) -> list[dict]:
    """Copy PRIMARY/SUPP from a position's opening deal onto its closing deal.

    Broker stop closes carry comments like ``[sl 4422.24]`` with no kind tag,
    which used to show as a fake third kind (OTHER)."""
    kind_by_pos: dict[str, str] = {}
    for o in orders:
        pid = str(o.get("position_id") or "")
        k = trade_kind(str(o.get("comment") or ""))
        if pid and k in ("PRIMARY", "SUPP") and (int(o.get("entry") or 0) == 0 or pid not in kind_by_pos):
            kind_by_pos[pid] = k
    for o in orders:
        pid = str(o.get("position_id") or "")
        if pid in kind_by_pos:
            o["kind"] = kind_by_pos[pid]
        comment_l = str(o.get("comment") or "").lower()
        if int(o.get("entry") or 0) == 1:
            o["deal_role"] = "SL_EXIT" if "[sl" in comment_l else "ORANGE_EXIT" if "onyxion-exit" in comment_l else "EXIT"
        else:
            o["deal_role"] = "OPEN"
    return orders


def build_trades_from_deals(raw_deals: list[dict]) -> list[dict]:
    """Pair DEAL_ENTRY_IN / OUT by position_id into PRIMARY/SUPP round-trips."""
    by_pos: dict = {}
    for d in raw_deals:
        pid = str(d.get("position_id") or d.get("order") or d.get("ticket") or "")
        if pid:
            by_pos.setdefault(pid, []).append(d)
    trades = []
    for pid, deals in by_pos.items():
        deals = sorted(deals, key=lambda x: str(x.get("time") or ""))
        opens = [d for d in deals if int(d.get("entry") or 0) == 0]
        closes = [d for d in deals if int(d.get("entry") or 0) == 1]
        if not opens:
            continue
        op = opens[0]
        kind = next((trade_kind(d.get("comment") or "") for d in deals
                     if trade_kind(d.get("comment") or "") != "OTHER"), "OTHER")
        if kind not in ("PRIMARY", "SUPP"):
            continue
        tr = {
            "position_id": pid, "ticket": op.get("ticket"), "kind": kind,
            "side": op.get("side") or "BUY", "volume": op.get("volume"),
            "open_time": op.get("time"), "open_price": op.get("price"),
            "open_comment": op.get("comment") or "", "magic": op.get("magic"),
            "status": "open", "close_time": None, "close_price": None, "profit": 0.0,
            "sl_comment": "", "broker_comment": "", "exit_reason": "OPEN",
        }
        ot = _parse_bar_ts(op.get("time"))
        if ot is not None:
            tr["bar_time"] = datetime.fromtimestamp(int(ot // 900) * 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if closes:
            cl = closes[-1]
            comment = cl.get("comment") or ""
            cl_l = comment.lower()
            tr.update({
                "status": "closed", "close_time": cl.get("time"), "close_price": cl.get("price"),
                "profit": round(float(cl.get("profit") or 0), 2),
                "broker_comment": comment, "sl_comment": comment, "ticket_close": cl.get("ticket"),
                "exit_reason": ("ORANGE_HISTOGRAM_EXIT" if "onyxion-exit" in cl_l
                                else "ENGINE_STOP" if "partial-stop" in cl_l
                                else "BROKER_STOP" if "[sl" in cl_l else "MT5_CLOSE"),
            })
        trades.append(tr)
    trades.sort(key=lambda t: str(t.get("open_time") or ""))
    return trades


def annotate_bars(bars: list[dict], trades: list[dict] | None = None, *,
                  deploy_skip_times: list[str] | None = None) -> list[dict]:
    """Add zone, gate flags and a plain-language reason to each bar.

    These reasons are the fallback for bars the engine never scored (warm-up
    history); collect_broker_snapshot overlays the engine's own decision on
    every bar it did score.
    """
    trades = trades or []
    th = mt5_live_engine.effective_hist_thresh()
    every = mt5_live_engine.effective_every_candle()
    skip_keys = {normalize_bar_time(t) for t in (deploy_skip_times or []) if normalize_bar_time(t)}
    marker = read_deploy_skip_bar()
    if marker and normalize_bar_time(marker.get("skip_bar_time")):
        skip_keys.add(normalize_bar_time(marker.get("skip_bar_time")))
    out = []
    for i, b in enumerate(bars):
        prev = bars[i - 1] if i > 0 else None
        hist = float(b.get("hist") or 0)
        zone = "green" if hist >= th else ("red" if hist <= -th else "amber")
        xt = float(b["xtrend"]) if b.get("xtrend") is not None else None
        h, l = float(b.get("signal_high", b["high"])), float(b.get("signal_low", b["low"]))
        body_hi = body_lo = None
        broke_buy = broke_sell = xt_buy = xt_sell = False
        if prev is not None:
            po, pc = float(prev.get("signal_open", prev["open"])), float(prev.get("signal_close", prev["close"]))
            body_hi, body_lo = max(po, pc), min(po, pc)
            broke_buy, broke_sell = h > body_hi, l < body_lo
        if xt is not None:
            xt_buy, xt_sell = l > xt, h < xt
        t0 = _parse_bar_ts(b.get("time"))
        bar_trades = [tr for tr in trades
                      if t0 is not None and (ot := _parse_bar_ts(tr.get("open_time"))) is not None
                      and t0 <= ot < t0 + 900]
        reasons = []
        action = "NONE"
        if bar_trades:
            action = ", ".join(f"{t.get('kind')} {t.get('side')}" for t in bar_trades)
            reasons.append(f"Trade taken: {action}")
        elif zone == "amber":
            reasons.append("Amber histogram - flatten / no new entries")
        else:
            buy = zone == "green"
            broke, clear = (broke_buy, xt_buy) if buy else (broke_sell, xt_sell)
            side = "BUY" if buy else "SELL"
            if not broke:
                reasons.append(f"SKIP {side}: did not break previous body ({body_hi if buy else body_lo})")
            elif not clear:
                reasons.append(f"SKIP {side}: candle not clear of X-Trend ({xt})")
            else:
                reasons.append(f"Body + X-Trend gates OK for {side} - no deal on this bar"
                               + ("" if every else " (classic mode: needs amber arm / run state)"))
        bar_key = normalize_bar_time(b.get("time"))
        deploy_skip = bool(bar_key and bar_key in skip_keys)
        if deploy_skip:
            reasons.insert(0, "DEPLOY SKIP: no new entries this candle (code update; positions/SL preserved)")
        row = dict(b)
        row.update({
            "zone": zone, "prev_body_high": body_hi, "prev_body_low": body_lo,
            "broke_prev_body_buy": broke_buy, "broke_prev_body_sell": broke_sell,
            "xt_clear_buy": xt_buy, "xt_clear_sell": xt_sell,
            "action": action, "deploy_skip": deploy_skip,
            "why": " | ".join(reasons) if reasons else "-",
            "trades_on_bar": [{"kind": t.get("kind"), "side": t.get("side"),
                               "open_price": t.get("open_price"), "close_price": t.get("close_price"),
                               "profit": t.get("profit")} for t in bar_trades],
        })
        out.append(row)
    return out


def _parse_bar_ts(ts) -> float | None:
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            return float(ts)
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_broker_snapshot(cfg: dict, symbol: str) -> dict:
    """Account + positions + round-trip trades + annotated bars for the live desk."""
    info = mt5.account_info()
    terminal = mt5.terminal_info()
    account = {}
    if info is not None:
        account = {
            "login": info.login, "server": info.server, "balance": info.balance,
            "equity": info.equity, "margin": info.margin, "margin_free": info.margin_free,
            "profit": info.profit, "trade_allowed": getattr(info, "trade_allowed", None),
            "trade_expert": getattr(info, "trade_expert", None),
            # The terminal's Algo Trading switch must also be on for order_send().
            "terminal_trade_allowed": getattr(terminal, "trade_allowed", None),
            "leverage": info.leverage, "currency": info.currency, "trade_mode": info.trade_mode,
            "company": info.company, "name": info.name,
        }
    magic = int(cfg["magic"])
    positions = [{
        "ticket": p.ticket, "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
        "volume": p.volume,
        "open_time": datetime.fromtimestamp(int(getattr(p, "time", 0) or 0), tz=timezone.utc).isoformat()
        if getattr(p, "time", 0) else None,
        "price_open": p.price_open, "sl": p.sl, "tp": p.tp, "profit": p.profit,
        "magic": int(p.magic), "symbol": p.symbol,
        "comment": getattr(p, "comment", "") or "", "kind": trade_kind(getattr(p, "comment", "") or ""),
    } for p in mt5.positions_get(symbol=symbol) or []]

    orders = []
    try:
        to_dt = datetime.now() + timedelta(days=1)
        for d in mt5.history_deals_get(to_dt - timedelta(days=14), to_dt) or []:
            if (getattr(d, "symbol", "") or "") != symbol:
                continue
            comment = d.comment or ""
            orders.append({
                "time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(), "type": "DEAL",
                "side": "BUY" if d.type == getattr(mt5, "DEAL_TYPE_BUY", 0) else "SELL",
                "volume": d.volume, "price": d.price, "ticket": d.ticket, "order": d.order,
                "position_id": int(getattr(d, "position_id", 0) or 0), "comment": comment,
                "profit": d.profit, "commission": getattr(d, "commission", 0) or 0,
                "magic": int(getattr(d, "magic", 0) or 0), "kind": trade_kind(comment),
                "entry": int(getattr(d, "entry", 0) or 0),
            })
        ours = [o for o in orders if o["magic"] == magic]
        orders = [o for o in enrich_deal_kinds(ours or orders)[-400:] if o.get("kind") in ("PRIMARY", "SUPP")]
    except Exception as e:
        orders = [{"time": "", "type": "ERROR", "side": "", "volume": 0, "price": 0, "ticket": "",
                   "comment": str(e), "profit": 0, "kind": "OTHER", "entry": 0, "position_id": 0}]

    trades = build_trades_from_deals(orders)
    runtime = cfg.get("_mt5_runtime") or {}
    skip_times = [str(runtime["deploy_skip_bar"])] if runtime.get("deploy_skip_bar") else []
    bars = annotate_bars(collect_bars(symbol, 120), trades, deploy_skip_times=skip_times)
    decision_records = runtime.get("decision_records") or []
    by_bar = {str(r.get("bar_time")): r for r in decision_records if r.get("bar_time")}
    for bar in bars:
        d = by_bar.get(str(bar.get("time")))
        if not d:
            continue
        bar["action"] = d.get("action") or "NONE"
        bar["filled_action"] = d.get("filled_action")
        bar["decision_hist"], bar["decision_histcolor"] = d.get("hist"), d.get("histcolor")
        bar["decision_zone"] = d.get("zone")
        bar["decision_reason"] = d.get("decision_reason") or d.get("why")
        if d.get("why") or d.get("decision_reason"):
            bar["why"] = d.get("why") or d.get("decision_reason")
        if d.get("sl") is not None:
            bar["sl"] = d.get("sl")

    last_bar = bars[-1] if bars else {}
    live = cfg.get("_mt5_live_result") or {}
    now_iso, now_unix = _now_iso(), int(datetime.now(timezone.utc).timestamp())
    tick_bid, tick_ask = last_bar.get("tick_bid"), last_bar.get("tick_ask")
    tick_time, tick_time_unix = last_bar.get("tick_time"), last_bar.get("tick_time_unix")
    if tick_bid is None or tick_ask is None or tick_time is None:
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None:
                tick_bid = round(float(getattr(tick, "bid", 0.0) or 0.0), 3) or None
                tick_ask = round(float(getattr(tick, "ask", 0.0) or 0.0), 3) or None
                tick_unix = int(getattr(tick, "time", 0) or 0)
                tick_time_unix = tick_unix or now_unix
                tick_time = datetime.fromtimestamp(tick_time_unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            tick_time, tick_time_unix = tick_time or now_iso, tick_time_unix or now_unix

    sl_trail = [{"time": r.get("bar_time"), "ticket": None, "side": r.get("filled_action") or r.get("action"),
                 "volume": None, "price_open": None, "sl": r.get("sl"), "tp": None, "profit": None,
                 "kind": "ENGINE", "source": "decision", "sl_changed": True}
                for r in decision_records if r.get("sl") is not None and r.get("bar_time")]
    sl_trail += [{"time": now_iso, "ticket": p["ticket"], "side": p["side"], "volume": p["volume"],
                  "price_open": p["price_open"], "sl": p["sl"], "tp": p["tp"], "profit": p["profit"],
                  "kind": p["kind"], "source": "position"} for p in positions]
    if live.get("sl_updated") is not None:
        sl_trail.append({"time": now_iso, "ticket": None, "side": live.get("filled_action") or live.get("action"),
                         "volume": None, "price_open": None, "sl": live.get("sl_updated"), "tp": None,
                         "profit": None, "kind": "ENGINE", "source": "engine", "sl_changed": bool(live.get("sl_changed"))})
    ohlc_records = [{
        "time": bar.get("time"), "raw_open": bar.get("raw_open"), "raw_high": bar.get("raw_high"),
        "raw_low": bar.get("raw_low"), "raw_close": bar.get("raw_close"),
        "ha_open": bar.get("open"), "ha_high": bar.get("high"), "ha_low": bar.get("low"), "ha_close": bar.get("close"),
        "xtrend": bar.get("xtrend"), "hist": bar.get("hist"), "zone": bar.get("zone") or bar.get("histcolor"),
        "action": bar.get("action"),
        "sl": live.get("sl_updated") if bar is bars[-1] else bar.get("sl"), "forming": bar.get("forming"),
    } for bar in bars[-120:]]
    xt_source = (bars[-1].get("xtrend_source") if bars else None) or os.environ.get("XTREND_SOURCE") or "supertrend"
    bridge = {
        "host": socket.gethostname(), "poll_seconds": cfg.get("poll"), "model": cfg.get("model"),
        "magic": magic, "expected_login": cfg.get("login"),
        "lot": mt5_live_engine.effective_volume(),
        "live_controls": cfg.get("_live_controls") or {},
        "live_controls_source": cfg.get("_live_controls_source") or "env",
        "execution_source": "mt5_bars", "execution_mode": "MT5 BARS",
        "mt5_connection": "ONLINE", "connection_error": None,
        "last_closed_bar": cfg.get("_mt5_last_closed_time"),
        "engine_sl": live.get("sl_updated"), "engine_sl_changed": live.get("sl_changed"),
        "engine_mode": live.get("mode"),
        "hist_thresh": mt5_live_engine.effective_hist_thresh(),
        "entry_every_candle": mt5_live_engine.effective_every_candle(),
        "tsl_atr_mult": mt5_live_engine.effective_tsl_atr_mult(),
        "tick_bid": tick_bid, "tick_ask": tick_ask, "tick_time": tick_time, "tick_time_unix": tick_time_unix,
        "feed_updated_at": now_iso, "server_time_utc": now_iso, "time_tz": "UTC",
        "recorded_value_fields": "HA_OHLC,RAW_OHLC,HIST,X_TREND,SL,TICK",
        "recorded_value_count": len(runtime.get("bar_records") or []),
        "recorded_decision_count": len(decision_records),
        "decision_records": decision_records[-512:],
        "pending_bar_time": runtime.get("pending_bar_time"),
        "pending_action": (runtime.get("pending_result") or {}).get("action"),
        "engine_action": live.get("filled_action") or live.get("action"),
        "engine_reason": live.get("why"),
        "xtrend_source": xt_source,
    }
    return {
        "account": account, "positions": positions, "orders": orders, "trades": trades, "bars": bars,
        "analysis": analyze_trades(trades), "sl_trail": sl_trail, "ohlc_records": ohlc_records,
        "symbol": symbol, "tick_bid": tick_bid, "tick_ask": tick_ask, "tick_time": tick_time,
        "tick_time_unix": tick_time_unix, "feed_updated_at": now_iso, "values_updated_at": now_iso,
        "server_time_utc": now_iso, "server_time_unix": now_unix, "time_tz": "UTC",
        "bridge": bridge, "xtrend_source": xt_source,
        "bar_time": last_bar.get("time"), "bar_time_unix": last_bar.get("time_unix"), "note": "ok",
    }


def offline_broker_snapshot(cfg: dict, reason: str) -> dict:
    """Keep the desk informed while the MT5 terminal is unavailable."""
    runtime = cfg.get("_mt5_runtime") or {}
    return {
        "account": {}, "positions": [], "orders": [], "trades": [], "bars": [], "analysis": {},
        "symbol": cfg.get("symbol"),
        "bridge": {
            "host": socket.gethostname(), "poll_seconds": cfg.get("poll"), "model": cfg.get("model"),
            "magic": int(cfg.get("magic") or 0), "expected_login": cfg.get("login"),
            "execution_source": "mt5_bars", "execution_mode": "MT5 BARS",
            "mt5_connection": "OFFLINE", "connection_error": str(reason)[:300],
            "last_closed_bar": cfg.get("_mt5_last_closed_time"),
            "recorded_value_count": len(runtime.get("bar_records") or []),
            "recorded_decision_count": len(runtime.get("decision_records") or []),
            "decision_records": (runtime.get("decision_records") or [])[-512:],
            "pending_bar_time": runtime.get("pending_bar_time"),
            "pending_action": (runtime.get("pending_result") or {}).get("action"),
            "engine_action": None, "engine_reason": str(reason)[:300],
        },
        "bar_time": None, "note": "bridge online; MT5 terminal unavailable",
    }


def write_local_status(cfg: dict, snapshot: dict, heartbeat_ok: bool) -> None:
    """state/bridge_status.json - read by github_update_agent for the desk's Ali PC panel."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        acct = snapshot.get("account") or {}
        bridge = snapshot.get("bridge") or {}
        tick_unix = snapshot.get("tick_time_unix")
        now = datetime.now(timezone.utc)
        payload = {
            "written_at": now.isoformat(),
            "login": acct.get("login"), "symbol": snapshot.get("symbol"),
            "mt5_connection": bridge.get("mt5_connection"),
            "trade_allowed": acct.get("trade_allowed"),
            "terminal_trade_allowed": acct.get("terminal_trade_allowed"),
            "tick_time": snapshot.get("tick_time"),
            "tick_age_sec": (int(now.timestamp()) - int(tick_unix)) if tick_unix else None,
            "last_closed_bar": bridge.get("last_closed_bar"),
            "n_positions": len(snapshot.get("positions") or []),
            "pending_bar_time": bridge.get("pending_bar_time"),
            "heartbeat_ok": bool(heartbeat_ok),
            "lot": bridge.get("lot"), "engine_mode": bridge.get("engine_mode"),
            "live_controls_source": bridge.get("live_controls_source"),
        }
        tmp = STATE_DIR / (STATUS_FILE_NAME + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(tmp, STATE_DIR / STATUS_FILE_NAME)
    except Exception as exc:
        log(f"local status write failed: {exc}")


def push_broker_heartbeat(cfg: dict, symbol: str) -> None:
    try:
        payload = collect_broker_snapshot(cfg, symbol)
    except Exception as exc:
        payload = offline_broker_snapshot(cfg, str(exc))
    ok = False
    try:
        http_post_json(cfg["lab_url"].rstrip("/") + "/api/broker/heartbeat", payload, timeout=20)
        ok = True
    except Exception as e:
        log(f"heartbeat push failed: {e}")
    write_local_status(cfg, payload, ok)


def push_broker_execution(cfg: dict, symbol: str, bar_time, action, *, ok: bool, results: list,
                          note: str = "", had_broker_orders: bool = False) -> None:
    """Report a consumed (or abandoned) bar to the desk's execution audit."""
    try:
        snap = collect_broker_snapshot(cfg, symbol)
        payload = {
            "bar_time": bar_time, "action": action, "ok": bool(ok), "results": list(results or []),
            "account": snap.get("account") or {}, "positions": snap.get("positions") or [],
            "symbol": symbol, "note": note or "", "had_broker_orders": bool(had_broker_orders),
            "bridge": snap.get("bridge") or {},
        }
        resp = http_post_json(cfg["lab_url"].rstrip("/") + "/api/broker/execution", payload, timeout=30)
        log(f"execution push bar={bar_time} ok={ok} n_results={len(payload['results'])} "
            f"matched={resp.get('matched')} mt5_status={resp.get('mt5_status')}")
    except Exception as e:
        log(f"execution push failed: {e}")


# --------------------------------------------------------------------------- #
# MT5 helpers                                                                  #
# --------------------------------------------------------------------------- #

def _tradable(name: str) -> bool:
    """A symbol the account may actually open a position on (not quote-only)."""
    info = mt5.symbol_info(name)
    return info is not None and info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL


def resolve_symbol(preferred: str) -> str | None:
    for name in (preferred, "XAUUSDm", "XAUUSD", "XAUUSD+", "XAUUSD.a", "GOLD"):
        if _tradable(name):
            mt5.symbol_select(name, True)
            return name
    for s in mt5.symbols_get() or []:
        n = s.name.upper()
        if "XAU" in n and "USD" in n and _tradable(s.name):
            mt5.symbol_select(s.name, True)
            return s.name
    return None


def positions_for_magic(symbol: str, magic: int) -> list:
    return [p for p in mt5.positions_get(symbol=symbol) or [] if int(p.magic) == magic]


# --------------------------------------------------------------------------- #
# Engine state on disk                                                         #
# --------------------------------------------------------------------------- #

def _engine_state_file(model: str) -> Path:
    return STATE_DIR / f"{str(model or 'ASIM').strip().lower()}_mt5_engine_state.json"


def save_local_engine(engine: Mt5LiveEngine, runtime: dict, model: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"execution_source": "mt5_bars", "model": str(model).upper(),
               "engine": engine.snapshot(), "runtime": runtime}
    state_file = _engine_state_file(model)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, state_file)


def _new_runtime(last_closed) -> dict:
    return {"last_closed_time": last_closed, "pending_bar_time": None, "pending_orders": [],
            "completed_orders": [], "entry_sent_bars": [], "last_result": None,
            "bar_records": [], "decision_records": [], "reconcile_log": []}


def load_local_engine(bars: list[dict], balance: float, model: str) -> tuple[Mt5LiveEngine, dict]:
    engine = Mt5LiveEngine()
    state_file = _engine_state_file(model)
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if data.get("model") not in (None, str(model).upper()):
                raise ValueError("state belongs to a different model namespace")
            engine.restore(data.get("engine") or {})
            runtime = data.get("runtime") or {}
            for k, v in _new_runtime(engine.last_closed_time).items():
                runtime.setdefault(k, v)
            return engine, runtime
        except Exception as exc:
            log(f"local MT5 engine state invalid - rebuilding: {exc}")
    closed = [b for b in bars if not b.get("forming")]
    engine.warmup(closed, balance=balance)
    last = closed[-1].get("time") if closed else None
    runtime = _new_runtime(last)
    save_local_engine(engine, runtime, model)
    log(f"Local MT5 engine warmed without orders through bar={last}")
    return engine, runtime


def record_mt5_values(runtime: dict, bars: list[dict]) -> None:
    """Keep the exact displayed HA/raw/indicator values for every MT5 bar."""
    keys = ("time", "open", "high", "low", "close", "raw_open", "raw_high", "raw_low", "raw_close",
            "candle_type", "hist", "histcolor", "xtrend", "forming", "action", "why")
    existing = {str(r.get("time")): r for r in (runtime.get("bar_records") or []) if r.get("time")}
    for bar in bars:
        if bar.get("time"):
            existing[str(bar["time"])] = {k: bar.get(k) for k in keys}
    runtime["bar_records"] = sorted(existing.values(), key=lambda r: str(r.get("time") or ""))[-MAX_LOCAL_BAR_RECORDS:]


def normalize_bar_time(value) -> str:
    """Canonical UTC bar key so Z vs +00:00 cannot re-trigger the same candle."""
    if value is None:
        return ""
    text = str(value).strip().replace(" ", "T")
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return str(value).strip()
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


ENTRY_ORDER_TYPES = {"ORDER_TYPE_BUY", "ORDER_TYPE_SELL"}


def _strip_entry_orders(orders: list[dict] | None) -> list[dict]:
    return [o for o in (orders or []) if str(o.get("actionType") or "").upper() not in ENTRY_ORDER_TYPES]


def _mark_entry_sent(runtime: dict, bar_time) -> None:
    key = normalize_bar_time(bar_time)
    if not key:
        return
    sent = [normalize_bar_time(x) for x in (runtime.get("entry_sent_bars") or [])]
    if key not in sent:
        sent.append(key)
    runtime["entry_sent_bars"] = sent[-512:]


def _entry_already_sent(runtime: dict, bar_time) -> bool:
    key = normalize_bar_time(bar_time)
    return bool(key) and key in {normalize_bar_time(x) for x in (runtime.get("entry_sent_bars") or [])}


def _entry_bar_is_stale(bar_time) -> bool:
    """A catch-up replay must trail stops and run the amber flatten, but never
    open a position on an hours-old signal at today's price."""
    opened = _parse_bar_ts(bar_time)
    return opened is not None and datetime.now(timezone.utc).timestamp() - (opened + 900) > STALE_ENTRY_SECONDS


# ---- deploy-skip marker (written by the candle-safe updater) ---------------

def _deploy_skip_path() -> Path:
    return STATE_DIR / "deploy_skip_bar.json"


def read_deploy_skip_bar() -> dict | None:
    path = _deploy_skip_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clear_deploy_skip_bar() -> None:
    try:
        _deploy_skip_path().unlink(missing_ok=True)
    except OSError:
        pass


def _should_skip_entries_for_deploy(bar_time) -> bool:
    """True = no new entries on this bar after a live code update. Never flattens."""
    data = read_deploy_skip_bar()
    if not data:
        return False
    skip_key, bar_key = normalize_bar_time(data.get("skip_bar_time")), normalize_bar_time(bar_time)
    if not skip_key or not bar_key:
        return False
    if bar_key == skip_key:
        return True
    if bar_key > skip_key:
        clear_deploy_skip_bar()
    return False


# --------------------------------------------------------------------------- #
# Engine result -> broker instructions                                         #
# --------------------------------------------------------------------------- #

def engine_order_hints(result: dict, symbol: str, *, allow_entries: bool = True) -> list[dict]:
    """Translate one engine result into MT5 instructions."""
    orders: list[dict] = []
    vol = float(result.get("fill_lot") or mt5_live_engine.effective_volume())
    active_sl = result.get("sl_updated", result.get("sl"))
    filled = result.get("filled_action") or (
        result.get("action") if result.get("action") in ("BUY", "SELL", "BUY_ADD", "SELL_ADD") else None)
    # Never re-fire a market entry from a replayed duplicate bar.
    if allow_entries and not result.get("duplicate_bar") and filled in ("BUY", "BUY_ADD", "SELL", "SELL_ADD"):
        # The new ticket's OWN stop (fill_sl), not the run's active stop: with
        # stacked tickets those differ, and sending the primary's tighter stop
        # with an add made the broker close the add before the engine knew.
        entry_sl = result.get("fill_sl")
        if entry_sl is None:
            entry_sl = active_sl
        orders.append({
            "actionType": "ORDER_TYPE_BUY" if "BUY" in filled else "ORDER_TYPE_SELL",
            "symbol": symbol, "volume": vol, "sl": entry_sl, "stopLoss": entry_sl,
            "kind": "PRIMARY" if filled in ("BUY", "SELL") else "SUPP", "engine_action": filled,
        })
    if result.get("action") == "EXIT" or (result.get("sl_exits") and result.get("n_total", 1) == 0):
        orders.append({"actionType": "POSITIONS_CLOSE_SYMBOL", "symbol": symbol})
    else:
        closed = ([result["closed_primary"]] if result.get("closed_primary") else []) + list(result.get("closed_supps") or [])
        for item in closed:
            orders.append({"actionType": "POSITIONS_CLOSE_PARTIAL_SYMBOL", "symbol": symbol,
                           "volume": float(item.get("lot") or vol), "entry": item.get("entry"),
                           "ticket": int(item.get("ticket") or 0), "exit": item.get("exit"),
                           "reason": item.get("reason")})
    if result.get("sl_changed") and active_sl is not None and result.get("n_total", 0) > 0:
        orders.append({"actionType": "SL_MODIFY", "symbol": symbol, "sl": active_sl, "stopLoss": active_sl,
                       "positions": result.get("open_positions") or []})
    return orders


def execute_local_orders(cfg: dict, symbol: str, runtime: dict, engine: Mt5LiveEngine | None = None) -> tuple[bool, list[dict]]:
    """Run the pending bar's instructions in order; stop at the first failure."""
    results: list[dict] = []
    completed = set(str(x) for x in runtime.get("completed_orders") or [])
    bar_time = runtime.get("pending_bar_time")
    for index, order in enumerate(runtime.get("pending_orders") or []):
        key = str(index)
        if key in completed:
            continue
        action_type = str(order.get("actionType") or "").upper()
        if action_type in ENTRY_ORDER_TYPES and _entry_already_sent(runtime, bar_time):
            log(f"  skip duplicate entry for bar={normalize_bar_time(bar_time)} action={order.get('engine_action')}")
            completed.add(key)
            runtime["completed_orders"] = sorted(completed)
            results.append({"ok": True, "skipped": True, "reason": "entry_already_sent_for_bar",
                            "bar_time": normalize_bar_time(bar_time)})
            continue
        ok, one = execute_broker_order(cfg, symbol, order)
        results.extend(one)
        if action_type in ENTRY_ORDER_TYPES:
            for r in one:
                if r.get("ok") and r.get("position_ticket") and engine is not None:
                    if engine.engine.bind_ticket(int(r["position_ticket"])):
                        log(f"  bound engine ticket -> #{r['position_ticket']}")
            if any(r.get("ok") for r in one):
                _mark_entry_sent(runtime, bar_time)
        if not ok:
            runtime["completed_orders"] = sorted(completed)
            return False, results
        completed.add(key)
        runtime["completed_orders"] = sorted(completed)
    runtime["completed_orders"] = sorted(completed)
    return True, results


def process_local_mt5_bar(cfg: dict, symbol: str, engine: Mt5LiveEngine, runtime: dict, bar: dict) -> bool:
    """Evaluate one newly closed MT5 candle and execute / retry its orders."""
    model = cfg["model"]
    bar_key = normalize_bar_time(bar.get("time")) or str(bar.get("time"))
    pending_key = normalize_bar_time(runtime.get("pending_bar_time"))
    if pending_key and pending_key == bar_key:
        # Retry: drop entry legs already sent for this bar, keep SL / close legs.
        if _entry_already_sent(runtime, bar_key):
            runtime["pending_orders"] = _strip_entry_orders(runtime.get("pending_orders"))
        ok, results = execute_local_orders(cfg, symbol, runtime, engine)
        if not ok:
            attempts = int(runtime.get("pending_attempts") or 0) + 1
            runtime["pending_attempts"] = attempts
            abandon = attempts >= MAX_PENDING_ATTEMPTS
            if abandon:
                runtime["pending_orders"], runtime["completed_orders"] = [], []
            save_local_engine(engine, runtime, model)
            push_broker_execution(cfg, symbol, bar_key, (runtime.get("pending_result") or {}).get("action"),
                                  ok=False, results=results,
                                  note=(f"pending MT5 bar legs abandoned after {attempts} attempts; engine advanced"
                                        if abandon else "pending MT5 bar order failed; retry retained"),
                                  had_broker_orders=True)
            if not abandon:
                log(f"Pending MT5 bar still not filled bar={bar_key} attempt={attempts}/{MAX_PENDING_ATTEMPTS}")
                return False
            log(f"Abandoning stuck pending legs bar={bar_key} after {attempts} attempts - advancing engine")
        result = runtime.get("pending_result") or {}
    else:
        tick = mt5.symbol_info_tick(symbol)
        execution_prices = None
        if tick is not None:
            execution_prices = {k: v for k, v in {"BUY": float(getattr(tick, "ask", 0) or 0),
                                                  "SELL": float(getattr(tick, "bid", 0) or 0)}.items() if v > 0} or None
        result = engine.push(bar, execution_prices=execution_prices)
        stale_entry = _entry_bar_is_stale(bar.get("time"))
        deploy_skip = _should_skip_entries_for_deploy(bar_key)
        allow_entries = (not result.get("duplicate_bar") and not _entry_already_sent(runtime, bar_key)
                         and not stale_entry and not deploy_skip)
        if result.get("duplicate_bar"):
            log(f"Duplicate engine bar={bar_key} - skipping market entry re-fire")
        if stale_entry:
            log(f"Catch-up bar={bar_key} closed too long ago - state/trail/exits applied, no new entry")
        if deploy_skip:
            log(f"DEPLOY SKIP bar={bar_key} - no new entries; open positions/SL preserved")
            clear_deploy_skip_bar()
            runtime["deploy_skip_bar"] = bar_key
        runtime["pending_bar_time"] = bar_key
        runtime["pending_result"] = result
        runtime["pending_orders"] = engine_order_hints(result, symbol, allow_entries=allow_entries)
        runtime["completed_orders"] = []
        save_local_engine(engine, runtime, model)
        ok, results = execute_local_orders(cfg, symbol, runtime, engine)
        if not ok:
            runtime["pending_attempts"] = 1
            save_local_engine(engine, runtime, model)
            push_broker_execution(cfg, symbol, bar_key, result.get("action"), ok=False, results=results,
                                  note="MT5 bar order failed; retry retained", had_broker_orders=True)
            log(f"MT5 bar order failed; retrying bar={bar_key}")
            return False

    action = result.get("filled_action") or result.get("action") or "NONE"
    log(f"MT5 closed bar={bar_key} action={action} orders={len(runtime.get('pending_orders') or [])} results={len(results)}")
    had_broker_orders = bool(runtime.get("pending_orders") or results)
    runtime["last_closed_time"] = bar_key
    runtime["last_result"] = result
    runtime.setdefault("decision_records", []).append({
        "bar_time": bar_key, "action": result.get("action"), "filled_action": result.get("filled_action"),
        "why": result.get("why") or result.get("decision_reason"), "decision_reason": result.get("decision_reason"),
        "hist": result.get("hist"), "histcolor": result.get("histcolor"), "zone": result.get("zone"),
        "ha_side": result.get("ha_side"), "position": result.get("position"),
        "n_units": result.get("n_units"), "n_supp": result.get("n_supp"), "n_total": result.get("n_total"),
        "execution_price_source": result.get("execution_price_source"),
        "fill_price": result.get("fill_price"), "fill_sl": result.get("fill_sl"),
        "mode": result.get("mode"), "atr": result.get("atr"), "tsl_distance": result.get("tsl_distance"),
        "hour_utc4": result.get("hour_utc4"), "xt_skips": result.get("xt_skips"),
        "sl": result.get("sl_updated", result.get("sl")), "sl_updated": result.get("sl_updated"),
        "orders": list(results or []),
    })
    runtime["decision_records"] = runtime["decision_records"][-2048:]
    for k in ("pending_bar_time", "pending_result", "pending_attempts"):
        runtime.pop(k, None)
    runtime["pending_orders"], runtime["completed_orders"] = [], []
    save_local_engine(engine, runtime, model)
    push_broker_execution(cfg, symbol, bar_key, result.get("action"), ok=True, results=results,
                          note="local MT5 closed-bar execution", had_broker_orders=had_broker_orders)
    return True


# --------------------------------------------------------------------------- #
# Broker orders                                                                #
# --------------------------------------------------------------------------- #

def order_send(request: dict) -> dict:
    r = mt5.order_send(request)
    if r is None:
        return {"ok": False, "error": f"order_send None retcode={mt5.last_error()}"}
    ok_codes = {
        mt5.TRADE_RETCODE_DONE,
        getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", -1),
        getattr(mt5, "TRADE_RETCODE_PLACED", -2),
        # 10025 NO_CHANGES: the broker already holds this SL. Treating it as a
        # failure once froze the bar pipeline on a stop that was already right.
        getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025),
    }
    return {"ok": r.retcode in ok_codes, "retcode": r.retcode, "deal": r.deal, "order": r.order,
            "price": getattr(r, "price", None), "requested_action": request.get("action"),
            "requested_type": request.get("type"), "requested_sl": request.get("sl"),
            "requested_position": request.get("position"), "requested_volume": request.get("volume"),
            "comment": r.comment, "volume": r.volume}


def filling_mode(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _close_request(symbol: str, position, volume: float, magic: int, deviation: int, comment: str) -> dict | None:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    is_buy = position.type == mt5.POSITION_TYPE_BUY
    return {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY, "position": position.ticket,
            "price": tick.bid if is_buy else tick.ask, "deviation": deviation, "magic": magic,
            "comment": comment, "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling_mode(symbol)}


def close_all(symbol: str, magic: int, deviation: int) -> list:
    results = []
    for p in positions_for_magic(symbol, magic):
        req = _close_request(symbol, p, p.volume, magic, deviation, "onyxion-exit")
        results.append(order_send(req) if req else {"ok": False, "error": "no tick"})
    return results


def close_position(symbol: str, position, volume: float, magic: int, deviation: int) -> dict:
    volume = min(float(volume), float(position.volume))
    if volume <= 0:
        return {"ok": True, "noop": True}
    req = _close_request(symbol, position, volume, magic, deviation, "onyxion-partial-stop")
    return order_send(req) if req else {"ok": False, "error": "no tick"}


def _same_broker_price(symbol: str, a, b) -> bool:
    try:
        left, right = float(a or 0.0), float(b or 0.0)
    except (TypeError, ValueError):
        return False
    if left <= 0.0 or right <= 0.0:
        return False
    info = mt5.symbol_info(symbol)
    digits = int(getattr(info, "digits", 2) or 2) if info is not None else 2
    return round(left, digits) == round(right, digits)


def _broker_safe_sl(symbol: str, side: str, sl: float, configured_floor: float = 0.0) -> float:
    """Keep the engine's stop unless MT5 requires a wider broker-side distance."""
    info, tick = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return float(sl)
    point = float(getattr(info, "point", 0.0) or 0.0)
    min_distance = max(float(getattr(info, "trade_stops_level", 0.0) or 0.0) * point,
                       float(getattr(info, "trade_freeze_level", 0.0) or 0.0) * point,
                       float(configured_floor or 0.0))
    safe = min(float(sl), float(tick.bid) - min_distance) if side == "LONG" else max(float(sl), float(tick.ask) + min_distance)
    return round(safe, int(getattr(info, "digits", 2) or 2))


def modify_position_sl(symbol: str, position, sl: float, configured_floor: float = 0.0) -> dict:
    side = "LONG" if position.type == mt5.POSITION_TYPE_BUY else "SHORT"
    broker_sl = _broker_safe_sl(symbol, side, sl, configured_floor)
    if _same_broker_price(symbol, getattr(position, "sl", 0.0), broker_sl):
        return {"ok": True, "retcode": getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025), "skipped": True,
                "comment": "SL already at target", "requested_sl": broker_sl, "requested_position": position.ticket}
    return order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol, "position": position.ticket,
                       "sl": broker_sl, "tp": float(getattr(position, "tp", 0.0) or 0.0)})


def _position_ticket_for(result: dict) -> int:
    """MT5 position ticket created by a filled market order (hedging: = order ticket)."""
    order = int(result.get("order") or 0)
    if order and mt5.positions_get(ticket=order):
        return order
    deal = int(result.get("deal") or 0)
    if deal:
        for d in mt5.history_deals_get(ticket=deal) or []:
            pid = int(getattr(d, "position_id", 0) or 0)
            if pid:
                return pid
    return order


def open_unit(symbol: str, side: str, lot: float, magic: int, deviation: int, model: str,
              sl: float | None = None, configured_floor: float = 0.0, kind: str = "") -> dict:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"ok": False, "error": "no tick"}
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(lot),
           "type": mt5.ORDER_TYPE_BUY if side == "LONG" else mt5.ORDER_TYPE_SELL,
           "price": tick.ask if side == "LONG" else tick.bid, "deviation": deviation, "magic": magic,
           "comment": f"onyxion-{model.lower()}-{str(kind or 'OTHER').lower()}",
           "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling_mode(symbol)}
    if sl is not None:
        req["sl"] = _broker_safe_sl(symbol, side, sl, configured_floor)
    result = order_send(req)
    result["entry"] = True
    if result.get("ok"):
        result["position_ticket"] = _position_ticket_for(result)
    return result


def _position_sort_key(position) -> tuple:
    return (int(getattr(position, "time_msc", 0) or 0), int(getattr(position, "time", 0) or 0), int(position.ticket))


def _match_position(positions: list, ticket: int, entry) -> object | None:
    """The broker ticket this engine record refers to: by ticket, else nearest entry within tolerance."""
    if ticket:
        for p in positions:
            if int(p.ticket) == int(ticket):
                return p
    if entry is None or not positions:
        return None
    best = min(positions, key=lambda p: abs(float(p.price_open) - float(entry)))
    return best if abs(float(best.price_open) - float(entry)) <= ENTRY_MATCH_TOLERANCE else None


def execute_broker_order(cfg: dict, symbol: str, order: dict) -> tuple[bool, list]:
    """Execute one instruction. Returns (ok, per-order result dicts)."""
    kind = str(order.get("actionType") or "").upper()
    magic, deviation = cfg["magic"], cfg["deviation"]
    positions = sorted(positions_for_magic(symbol, magic), key=_position_sort_key)
    broker_floor = float(cfg.get("broker_min_stop_pts") or 0.0)

    if kind == "POSITIONS_CLOSE_SYMBOL":
        results = close_all(symbol, magic, deviation)
        for r in results:
            log(f"  close-symbol -> {r}")
        return (all(r.get("ok") for r in results) if results else True), results

    if kind == "POSITIONS_CLOSE_PARTIAL_SYMBOL":
        position = _match_position(positions, int(order.get("ticket") or 0), order.get("entry"))
        if position is None:
            # The broker's own stop already closed it (or nothing matches):
            # closing "the nearest" ticket instead would kill a different trade.
            log(f"  partial close: no broker ticket for engine entry {order.get('entry')} "
                f"(ticket {order.get('ticket') or '-'}) - already closed by broker")
            return True, [{"ok": True, "skipped": True, "reason": "already_closed_by_broker",
                           "entry": order.get("entry"), "ticket": order.get("ticket")}]
        result = close_position(symbol, position, float(order.get("volume") or cfg["lot"]), magic, deviation)
        log(f"  partial #{position.ticket} -> {result}")
        return bool(result.get("ok")), [result]

    if kind in ENTRY_ORDER_TYPES:
        result = open_unit(symbol, "LONG" if kind == "ORDER_TYPE_BUY" else "SHORT",
                           float(order.get("volume") or cfg["lot"]), magic, deviation, cfg["model"],
                           sl=order.get("sl") if order.get("sl") is not None else order.get("stopLoss"),
                           configured_floor=broker_floor, kind=order.get("kind") or "")
        log(f"  structured entry -> {result}")
        return bool(result.get("ok")), [result]

    if kind == "SL_MODIFY":
        if not positions:
            return True, []
        results = []
        unmatched = list(positions)
        for target in order.get("positions") or []:
            p = _match_position(unmatched, int(target.get("ticket") or 0), target.get("entry"))
            if p is None or target.get("sl") is None:
                continue
            unmatched.remove(p)
            results.append(modify_position_sl(symbol, p, float(target["sl"]), broker_floor))
        if unmatched:
            sl = order.get("sl") if order.get("sl") is not None else order.get("stopLoss")
            if sl is None and not results:
                return False, [{"ok": False, "error": "SL_MODIFY missing sl"}]
            if sl is not None:
                results.extend(modify_position_sl(symbol, p, float(sl), broker_floor) for p in unmatched)
        for r in results:
            log(f"  stop modify -> {r}")
        return all(r.get("ok") for r in results), results

    log(f"  reject unknown broker action {kind}")
    return False, [{"ok": False, "error": f"unknown actionType {kind}"}]


def reconcile_with_broker(engine: Mt5LiveEngine, runtime: dict, symbol: str, magic: int) -> None:
    """Drop engine tickets the broker no longer holds; warn about unknown broker tickets."""
    eng = engine.engine
    if not any(p.ticket for p in eng.positions):
        return
    broker = positions_for_magic(symbol, magic)
    alive = {int(p.ticket) for p in broker}
    tick = mt5.symbol_info_tick(symbol)
    mark = float(eng._prev_raw_close or 0.0)
    if tick is not None and tick.bid and tick.ask:
        mark = (float(tick.bid) + float(tick.ask)) / 2.0
    dropped = eng.reconcile(alive, mark)
    if dropped:
        for rec in dropped:
            log(f"reconcile: broker closed #{rec['ticket']} (entry {rec['entry']}) - engine ticket dropped")
        runtime.setdefault("reconcile_log", []).extend(
            {**rec, "time": _now_iso()} for rec in dropped)
        runtime["reconcile_log"] = runtime["reconcile_log"][-200:]
    known = {p.ticket for p in eng.positions if p.ticket}
    unknown = alive - known - set(runtime.get("_warned_unknown") or [])
    if unknown:
        log(f"reconcile: broker holds tickets the engine does not track: {sorted(unknown)} (manual or pre-restart)")
        runtime["_warned_unknown"] = sorted(set(runtime.get("_warned_unknown") or []) | unknown)[-50:]


# --------------------------------------------------------------------------- #
# MT5 session                                                                  #
# --------------------------------------------------------------------------- #

def connect(cfg: dict) -> None:
    path = (cfg.get("terminal") or "").strip() or None
    if path and not Path(path).is_file():
        raise SystemExit(f"MT5 terminal path missing: {path}")
    init_kwargs = {"login": cfg["login"], "password": cfg["password"], "server": cfg["server"], "timeout": 60000}
    if cfg.get("portable"):
        init_kwargs["portable"] = True
    ok = mt5.initialize(path, **init_kwargs) if path else mt5.initialize(**init_kwargs)
    if not ok:
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    info = mt5.account_info()
    if info is None:
        mt5.shutdown()
        raise SystemExit("account_info None after login")
    expected_server = str(cfg["server"]).strip()

    def mismatch(i) -> bool:
        return int(getattr(i, "login", 0) or 0) != int(cfg["login"]) or \
            str(getattr(i, "server", "") or "").strip().casefold() != expected_server.casefold()

    if mismatch(info):
        if not mt5.login(cfg["login"], password=cfg["password"], server=cfg["server"], timeout=60000):
            err = mt5.last_error()
            mt5.shutdown()
            raise SystemExit(f"MT5 login failed login={cfg['login']} server={cfg['server']}: {err}")
        info = mt5.account_info()
        if info is None or mismatch(info):
            actual = None if info is None else f"{info.login} / {info.server}"
            mt5.shutdown()
            raise SystemExit(f"MT5 session mismatch: expected {cfg['login']} / {expected_server}; connected {actual}")
    symbol = str(cfg.get("symbol") or "").strip()
    if symbol:
        if not mt5.symbol_select(symbol, True):
            err = mt5.last_error()
            mt5.shutdown()
            raise SystemExit(f"MT5 symbol select failed symbol={symbol}: {err}")
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None or getattr(symbol_info, "trade_mode", None) == getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0):
            mt5.shutdown()
            raise SystemExit(f"MT5 symbol unavailable or trading disabled symbol={symbol}")
    if info.trade_mode == 2:   # 0=demo, 1=contest, 2=real
        mt5.shutdown()
        raise SystemExit("REFUSED: account looks REAL (trade_mode=2). Demo only.")
    terminal_info = mt5.terminal_info()
    log(f"Connected {cfg['model']} login={info.login} server={info.server} balance={info.balance} "
        f"leverage=1:{info.leverage} mode={info.trade_mode} (0=demo) "
        f"terminal_trade_allowed={getattr(terminal_info, 'trade_allowed', None)}")


def sync_once_mt5_bars(cfg: dict, symbol: str) -> None:
    """Evaluate and execute only newly closed MT5 M15 candles."""
    model = cfg["model"]
    bars = collect_bars(symbol, count=160)
    closed = [b for b in bars if not b.get("forming")]
    if not closed:
        log("No closed MT5 M15 bar available")
        push_broker_heartbeat(cfg, symbol)
        return
    account = mt5.account_info()
    balance = float(getattr(account, "balance", 100.0) or 100.0)
    engine, runtime = load_local_engine(bars, balance, model)
    reconcile_with_broker(engine, runtime, symbol, int(cfg["magic"]))

    # SKIP_WEEKENDS=1: do not keep retrying a Friday entry over the weekend.
    if mt5_live_engine.effective_skip_weekends() and datetime.now(timezone.utc).weekday() >= 5:
        if runtime.get("pending_bar_time") or runtime.get("pending_orders"):
            for k in ("pending_bar_time", "pending_result", "pending_attempts"):
                runtime.pop(k, None)
            runtime["pending_orders"], runtime["completed_orders"] = [], []
            save_local_engine(engine, runtime, model)
            log("Market closed for weekend; cleared pending MT5 bar order")
        push_broker_heartbeat(cfg, symbol)
        return

    record_mt5_values(runtime, bars)
    save_local_engine(engine, runtime, model)
    cfg["_mt5_runtime"] = runtime
    if not runtime.get("pending_bar_time"):
        # Broker-confirmed realised balance is the risk gate's source of truth.
        engine.engine.balance = balance
    term = mt5.terminal_info()
    trade_ok = bool(term and getattr(term, "trade_allowed", False))

    if runtime.get("last_closed_time"):
        runtime["last_closed_time"] = normalize_bar_time(runtime["last_closed_time"])
    if runtime.get("pending_bar_time"):
        runtime["pending_bar_time"] = normalize_bar_time(runtime["pending_bar_time"])
    pending_time, last_time = runtime.get("pending_bar_time"), runtime.get("last_closed_time")
    closed_keys = {normalize_bar_time(b.get("time")) for b in closed}
    newest_closed = normalize_bar_time(closed[-1].get("time"))
    if not last_time and not pending_time:
        # A null last_closed must not replay 160 bars of history as fresh signals.
        runtime["last_closed_time"] = last_time = newest_closed
        save_local_engine(engine, runtime, model)
        log(f"Initialized empty last_closed_time to newest closed={newest_closed}")
    if pending_time and (pending_time not in closed_keys or (last_time and pending_time < last_time)
                         or pending_time < newest_closed):
        log(f"Clearing stale pending bar={pending_time} last_closed={last_time or None} newest={newest_closed}")
        for k in ("pending_bar_time", "pending_result", "pending_attempts"):
            runtime.pop(k, None)
        runtime["pending_orders"], runtime["completed_orders"] = [], []
        if not last_time:
            runtime["last_closed_time"] = newest_closed
        save_local_engine(engine, runtime, model)
        pending_time = None

    if pending_time:
        pending_bar = next((b for b in closed if normalize_bar_time(b.get("time")) == pending_time), {"time": pending_time})
        if not trade_ok:
            log("Algo Trading OFF in MT5 - pending local bar will retry")
        elif not process_local_mt5_bar(cfg, symbol, engine, runtime, pending_bar):
            cfg["_mt5_live_result"] = runtime.get("pending_result")
            push_broker_heartbeat(cfg, symbol)
            return

    last_key = normalize_bar_time(runtime.get("last_closed_time") or "")
    new_bars = [b for b in closed if not last_key or normalize_bar_time(b.get("time")) > last_key]
    if new_bars and not trade_ok:
        log("Algo Trading OFF in MT5 - closed bars remain pending")
    elif trade_ok:
        for bar in new_bars:
            if not process_local_mt5_bar(cfg, symbol, engine, runtime, bar):
                break
    cfg["_mt5_live_result"] = runtime.get("last_result")
    cfg["_mt5_last_closed_time"] = runtime.get("last_closed_time")
    push_broker_heartbeat(cfg, symbol)


def sync_once(cfg: dict) -> None:
    global _LAST_SYMBOL_LOGGED
    apply_live_controls(cfg)
    symbol = resolve_symbol(cfg["symbol"])
    if not symbol:
        # A closed/disconnected terminal is recoverable; the main loop reconnects.
        raise RuntimeError(f"Symbol not found for {cfg['symbol']}")
    cfg["symbol_resolved"] = symbol
    if symbol != _LAST_SYMBOL_LOGGED:
        info = mt5.symbol_info(symbol)
        log(f"Symbol {symbol} digits={info.digits} point={info.point}")
        _LAST_SYMBOL_LOGGED = symbol
    sync_once_mt5_bars(cfg, symbol)


def main():
    ap = argparse.ArgumentParser(description="Onyxion MT5 bridge (closed-bar execution)")
    ap.add_argument("--model", default="ASIM", choices=["ASIM", "asim"],
                    help="kept for the watchdog's command line; only ASIM exists")
    ap.add_argument("--once", action="store_true", help="single sync then exit")
    args = ap.parse_args()
    env = load_env(ENV_PATH)
    apply_engine_environment(env)
    cfg = build_cfg(env)

    try:
        connect(cfg)
    except (SystemExit, Exception) as exc:
        if "REFUSED" in str(exc) or args.once:
            raise
        log(f"MT5 unavailable at startup - bridge will keep retrying: {exc}")
        try:
            mt5.shutdown()
        except Exception:
            pass
        push_broker_heartbeat(cfg, cfg.get("symbol") or "")

    try:
        acquire_local_executor(cfg["model"])
    except Exception:
        mt5.shutdown()
        raise
    try:
        if args.once:
            sync_once(cfg)
            return
        log(f"Looping every {cfg['poll']}s - Ctrl+C to stop")
        last_reconnect = 0.0
        while True:
            try:
                sync_once(cfg)
            except (Exception, SystemExit) as e:
                log(f"ERROR sync: {e}")
                push_broker_heartbeat(cfg, cfg.get("symbol") or "")
                now = time.monotonic()
                if now - last_reconnect >= 10.0:
                    last_reconnect = now
                    try:
                        mt5.shutdown()
                        connect(cfg)
                        log("MT5 reconnect succeeded")
                    except SystemExit as reconnect_error:
                        if "REFUSED" in str(reconnect_error):
                            raise
                        log(f"MT5 reconnect pending: {reconnect_error}")
                    except Exception as reconnect_error:
                        log(f"MT5 reconnect pending: {reconnect_error}")
            time.sleep(cfg["poll"])
    finally:
        release_local_executor()
        mt5.shutdown()
        log("MT5 shutdown")


if __name__ == "__main__":
    main()
