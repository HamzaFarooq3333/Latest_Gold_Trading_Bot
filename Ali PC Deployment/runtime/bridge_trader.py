"""
Onyxion MT5 demo bridge — executes the local Demo engine on closed MT5 bars.

Usage:
  python bridge_trader.py --model HARD
  python bridge_trader.py --model SOFT
  python bridge_trader.py --model ASIM
  python bridge_trader.py --model DEMO  # closed MT5 M15 bars; AWS is display-only
  python bridge_trader.py --model TRADINGVIEW_DEMO
  python bridge_trader.py --model DEMO --once   # single sync then exit

Requires: MetaTrader 5 terminal running (or path in .env), package MetaTrader5.
Demo only. Credentials from metatrader5/.env (never commit).
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5
try:
    from mt5_live_engine import Mt5LiveEngine
except ModuleNotFoundError:
    from Vintage.metatrader5.python.mt5_live_engine import Mt5LiveEngine

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _resolve_env_path() -> Path:
    """Prefer .env beside the script (AWS flat layout), else parent (repo layout)."""
    for candidate in (HERE / ".env", ROOT / ".env", Path("C:/onyxion/.env")):
        if candidate.is_file():
            return candidate
    return HERE / ".env"


ENV_PATH = _resolve_env_path()
# Flat AWS layout (C:\\onyxion\\bridge_trader.py) keeps state/logs next to the script.
_DATA_ROOT = HERE if (HERE / ".env").is_file() or HERE.name.lower() == "onyxion" else ROOT
STATE_DIR = _DATA_ROOT / "state"
LOG_DIR = _DATA_ROOT / "logs"
# The local engine uses a 0.25pt stop. A broker may require a wider distance,
# so the bridge widens only the submitted broker-side stop.
DEMO_BROKER_MIN_STOP_PTS = float(os.environ.get("DEMO_BROKER_MIN_STOP_PTS", "0.30"))
ASIM_BROKER_MIN_STOP_PTS = float(
    os.environ.get("ASIM_BROKER_MIN_STOP_PTS", os.environ.get("BROKER_MIN_STOP_PTS", "0.30"))
)
# A pending bar's orders are retried, but never forever: the engine must reach
# newer bars to keep trailing stops and the amber flatten alive.
MAX_PENDING_ATTEMPTS = int(os.environ.get("MAX_PENDING_ATTEMPTS", "8"))
# Entries from candles that closed more than this long ago are not sent: a
# catch-up replay must not fill an old signal at the current market price.
STALE_ENTRY_SECONDS = float(os.environ.get("STALE_ENTRY_SECONDS", "1800"))
# Canonical MT5 calculation inputs. The EA uses the same closed-bar window.
MT5_CALC_WINDOW = 160
MT5_XTREND_PERIOD = 6
MT5_XTREND_MULT = 0.8
# XTREND_SOURCE: "supertrend" (HA SuperTrend 6/0.8) or "gaga" (KJ GagaTrend Pine).
XTREND_SOURCE = os.environ.get("XTREND_SOURCE", "supertrend").strip().lower()
MAX_LOCAL_BAR_RECORDS = 20000
LOCAL_ENGINE_STATE_FILE = STATE_DIR / "demo_mt5_engine_state.json"
LOCAL_EXECUTOR_LOCK_FILE = STATE_DIR / "demo_mt5_executor.lock"
_EXECUTOR_LOCK_HANDLE = None


def _local_engine_state_file(model: str) -> Path:
    return STATE_DIR / f"{str(model or 'DEMO').strip().lower()}_mt5_engine_state.json"


def _local_executor_lock_file(model: str) -> Path:
    return STATE_DIR / f"{str(model or 'DEMO').strip().lower()}_mt5_executor.lock"


def acquire_local_executor(model: str = "DEMO") -> None:
    """Allow exactly one bridge process per MT5-bar model to submit orders."""
    global _EXECUTOR_LOCK_HANDLE
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(_local_executor_lock_file(model), "a+", encoding="utf-8")
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
    except (OSError, IOError):
        handle.close()
        raise SystemExit(
            f"REFUSED: another {str(model).upper()} MT5-bar executor is already running"
        )
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
    out: dict[str, str] = {}
    if not path.is_file():
        raise SystemExit(
            f"Missing {path} — put .env next to bridge_trader.py "
            f"(e.g. C:\\onyxion\\.env) or in the parent folder"
        )
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def apply_engine_environment(env: dict[str, str]) -> None:
    """Load .env before constructing the rules engine."""
    os.environ.update(env)
    global Mt5LiveEngine
    module = importlib.import_module(Mt5LiveEngine.__module__)
    module = importlib.reload(module)
    Mt5LiveEngine = module.Mt5LiveEngine


def apply_live_controls(cfg: dict) -> None:
    """Pull dashboard live controls and apply to cfg + engine env (no restart)."""
    try:
        data = http_json(cfg["lab_url"].rstrip("/") + "/api/broker/controls", timeout=8)
        controls = (data or {}).get("controls") or {}
    except Exception as exc:
        log(f"live controls unavailable: {exc}")
        return
    if not controls:
        return
    prev = cfg.get("_live_controls") or {}
    mapped = {
        "VOLUME": ("lot", float),
        "TSL_PTS": None,
        # Percentage trailing stop (0.25 = 0.25%). Must be pulled from the desk
        # like every other control: values the desk serves overwrite .env each
        # cycle, so a key missing from this map cannot be changed from the
        # dashboard and would silently keep whatever .env happened to hold.
        "TSL_PCT": None,
        # Trailing stop in ticks - the authoritative stop setting.
        "TSL_TICKS": None,
        "TSL_TICK_SIZE": None,
        # ATR-sized stop multiplier and every-candle entry mode.
        "TSL_ATR_MULT": None,
        "ENTRY_EVERY_CANDLE": None,
        "BROKER_MIN_STOP_PTS": None,
        "STOP_SLIPPAGE_PTS": None,
        "SPREAD_COST": None,
        "TRAIL_EVERY_CANDLE": None,
        "TRAIL_ENTRY_BAR": None,
        "ENTRY_BAR_MODE": None,
        "DISABLE_STOP_LOSS": None,
        "MAXPOS": ("max_units", int),
        "MAX_SUPP": None,
        "BEST_LOT_MULT": None,
        "HIST_THRESH": None,
        "XTREND_GATE": None,
        "XTREND_GATE_SUPP": None,
        "XTREND_SOURCE": None,
        "SKIP_WEEKENDS": None,
    }
    changed = []
    for key, meta in mapped.items():
        if key not in controls:
            continue
        raw = controls[key]
        os.environ[key] = str(raw)
        if key == "VOLUME":
            os.environ["ASIM_MT5_LOT"] = str(raw)
            os.environ["MT5_LOT"] = str(raw)
        if meta is not None:
            field, caster = meta
            try:
                cfg[field] = caster(raw)
            except (TypeError, ValueError):
                pass
        if str(prev.get(key)) != str(raw):
            changed.append(f"{key}={raw}")
    cfg["_live_controls"] = {k: controls.get(k) for k in mapped}
    if changed:
        log("live controls applied: " + ", ".join(changed))


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    with open(LOG_DIR / f"bridge_{day}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def cfg_for(model: str, env: dict) -> dict:
    model = model.upper()
    if model == "HARD":
        return {
            "model": "HARD",
            "login": int(env["HARD_MT5_LOGIN"]),
            "password": env["HARD_MT5_PASSWORD"],
            "server": env["HARD_MT5_SERVER"],
            "magic": int(env.get("MT5_MAGIC_HARD", "110045")),
            "lab_url": env["HARD_LAB_URL"].rstrip("/") + "/",
            "lot": float(env.get("MT5_LOT", "0.01")),
            "symbol": env.get("MT5_SYMBOL", "XAUUSD"),
            "max_units": int(env.get("MT5_MAX_UNITS", "5")),
            "deviation": int(env.get("MT5_DEVIATION", "30")),
            "poll": int(env.get("MT5_POLL_SECONDS", "2")),
            "terminal": env.get("MT5_TERMINAL_PATH", ""),
        }
    if model == "SOFT":
        return {
            "model": "SOFT",
            "login": int(env["SOFT_MT5_LOGIN"]),
            "password": env["SOFT_MT5_PASSWORD"],
            "server": env["SOFT_MT5_SERVER"],
            "magic": int(env.get("MT5_MAGIC_SOFT", "115696")),
            "lab_url": env["SOFT_LAB_URL"].rstrip("/") + "/",
            "lot": float(env.get("MT5_LOT", "0.01")),
            "symbol": env.get("MT5_SYMBOL", "XAUUSD"),
            "max_units": int(env.get("MT5_MAX_UNITS", "5")),
            "deviation": int(env.get("MT5_DEVIATION", "30")),
            "poll": int(env.get("ASIM_MT5_POLL_SECONDS", env.get("MT5_POLL_SECONDS", "2"))),
            "terminal": env.get("MT5_TERMINAL_PATH", ""),
        }
    if model in ("ASIM", "ASIM_FB_AGENT"):
        # GCP only — hamza https://35.232.76.12  ali https://35.223.235.204
        lab_raw = (env.get("ASIM_LAB_URL") or "").strip()
        if not lab_raw:
            raise SystemExit(
                "ASIM_LAB_URL is required in .env (GCP only — "
                "hamza https://35.232.76.12  ali https://35.223.235.204)"
            )
        return {
            "model": "ASIM",
            "login": int(env["ASIM_MT5_LOGIN"]),
            "password": env["ASIM_MT5_PASSWORD"],
            "server": env["ASIM_MT5_SERVER"],
            "magic": int(env.get("MT5_MAGIC_ASIM", "126823")),
            "lab_url": lab_raw.rstrip("/") + "/",
            "lot": float(env.get("ASIM_MT5_LOT", env.get("VOLUME", env.get("MT5_LOT", "0.01")))),
            "symbol": env.get("ASIM_MT5_SYMBOL", env.get("MT5_SYMBOL", "XAUUSD")),
            "max_units": int(env.get("MT5_MAX_UNITS", "20")),
            "deviation": int(env.get("MT5_DEVIATION", "30")),
            "poll": int(env.get("MT5_POLL_SECONDS", "2")),
            "terminal": env.get("ASIM_MT5_TERMINAL_PATH", env.get("MT5_TERMINAL_PATH", "")),
            "portable": env.get(
                "ASIM_MT5_PORTABLE", env.get("MT5_PORTABLE", "1")
            ).strip().lower() in ("1", "true", "yes", "on"),
            "execution_source": "mt5_bars",
            "broker_min_stop_pts": float(
                env.get("ASIM_BROKER_MIN_STOP_PTS", env.get("BROKER_MIN_STOP_PTS", "0.30"))
            ),
        }
    if model in ("DEMO", "DEMO_TRADING"):
        # Dedicated latest Exness demo account; wired to Demo Trading AWS lab.
        return {
            "model": "DEMO",
            "login": int(env.get("DEMO_MT5_LOGIN", env["ASIM_MT5_LOGIN"])),
            "password": env.get("DEMO_MT5_PASSWORD", env["ASIM_MT5_PASSWORD"]),
            "server": env.get("DEMO_MT5_SERVER", env["ASIM_MT5_SERVER"]),
            "magic": int(env.get("MT5_MAGIC_DEMO", "126824")),
            "lab_url": env.get(
                "DEMO_LAB_URL",
                "https://azmbjjosjkia5h37iud55ai7nm0ftjzh.lambda-url.us-east-1.on.aws",
            ).rstrip("/") + "/",
            "lot": float(env.get("DEMO_MT5_LOT", env.get("ASIM_MT5_LOT", env.get("MT5_LOT", "0.01")))),
            "symbol": env.get("DEMO_MT5_SYMBOL", env.get("ASIM_MT5_SYMBOL", env.get("MT5_SYMBOL", "XAUUSDm"))),
            "max_units": int(env.get("MT5_MAX_UNITS", "20")),
            "deviation": int(env.get("MT5_DEVIATION", "30")),
            "poll": int(env.get("MT5_POLL_SECONDS", env.get("DEMO_MT5_POLL_SECONDS", "1"))),
            "terminal": env.get(
                "DEMO_MT5_TERMINAL_PATH",
                env.get("ASIM_MT5_TERMINAL_PATH", env.get("MT5_TERMINAL_PATH", "")),
            ),
            "portable": env.get(
                "DEMO_MT5_PORTABLE", env.get("MT5_PORTABLE", "1")
            ).strip().lower()
            in ("1", "true", "yes", "on"),
            "execution_source": "mt5_bars",
            "broker_min_stop_pts": DEMO_BROKER_MIN_STOP_PTS,
        }
    if model in ("TRADINGVIEW_DEMO", "TVDEMO", "TV_DEMO"):
        return {
            "model": "TRADINGVIEW_DEMO",
            "login": int(env["TRADINGVIEW_DEMO_MT5_LOGIN"]),
            "password": env["TRADINGVIEW_DEMO_MT5_PASSWORD"],
            "server": env["TRADINGVIEW_DEMO_MT5_SERVER"],
            "magic": int(env.get("MT5_MAGIC_TRADINGVIEW_DEMO", "126825")),
            "lab_url": env.get(
                "TRADINGVIEW_DEMO_LAB_URL",
                env.get(
                    "DEMO_LAB_URL",
                    "https://azmbjjosjkia5h37iud55ai7nm0ftjzh.lambda-url.us-east-1.on.aws",
                ),
            ).rstrip("/") + "/",
            "lot": float(env.get("TRADINGVIEW_DEMO_MT5_LOT", env.get("MT5_LOT", "0.01"))),
            "symbol": env.get("TRADINGVIEW_DEMO_MT5_SYMBOL", "XAUUSD+"),
            "max_units": int(env.get("MT5_MAX_UNITS", "20")),
            "deviation": int(env.get("MT5_DEVIATION", "30")),
            "poll": int(env.get("TRADINGVIEW_DEMO_MT5_POLL_SECONDS", env.get("MT5_POLL_SECONDS", "1"))),
            "terminal": env.get(
                "TRADINGVIEW_DEMO_MT5_TERMINAL_PATH",
                env.get("MT5_TERMINAL_PATH", ""),
            ),
        }
    raise SystemExit("model must be HARD, SOFT, ASIM, DEMO, or TRADINGVIEW_DEMO")


def _lab_ssl_context():
    """GCP desks use self-signed HTTPS; set ASIM_LAB_INSECURE=0 to enforce verify."""
    insecure = os.environ.get("ASIM_LAB_INSECURE", "1").strip().lower()
    if insecure in ("0", "false", "no"):
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_json(url: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_lab_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_lab_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wilder_rsi(closes: list[float], period: int = 3) -> list[float]:
    n = len(closes)
    rsi = [50.0] * n
    if n < 2:
        return rsi
    avg_g = avg_l = 0.0
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        if i == 1:
            avg_g, avg_l = g, l
        else:
            avg_g = avg_g + (g - avg_g) / period
            avg_l = avg_l + (l - avg_l) / period
        rsi[i] = 100.0 if avg_l <= 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return rsi


def _ema_span(src: list[float], span: int = 5) -> list[float]:
    if not src:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [src[0]]
    for i in range(1, len(src)):
        out.append(alpha * src[i] + (1.0 - alpha) * out[-1])
    return out


def hist_color(h: float, thresh: float | None = None) -> str:
    # Read HIST_THRESH at call time. The engine trusts the colour it is sent
    # (zone_from_sent), so a threshold fixed here would silently override
    # .env and the dashboard control no matter what they were set to.
    if thresh is None:
        try:
            thresh = float(os.environ.get("HIST_THRESH", "10"))
        except (TypeError, ValueError):
            thresh = 10.0
    if h >= thresh:
        return "green"
    if h <= -thresh:
        return "red"
    return "amber"


def trade_kind(comment: str) -> str:
    c = (comment or "").upper()
    if "PRIMARY" in c:
        return "PRIMARY"
    if "SUPP" in c or "ADD" in c:
        return "SUPP"
    return "OTHER"


def enrich_deal_kinds(orders: list[dict]) -> list[dict]:
    """Attach PRIMARY/SUPP from the open deal onto SL/TP closes of the same position.

    Broker stop closes use comments like ``[sl 4422.24]`` with no PRIMARY/SUPP tag,
    which previously showed as a fake third kind (OTHER). Strategy only has
    PRIMARY and SUPP — never OTHER as a trade type.
    """
    kind_by_pos: dict[str, str] = {}
    for o in orders:
        pid = str(o.get("position_id") or "")
        if not pid:
            continue
        k = trade_kind(str(o.get("comment") or ""))
        if k in ("PRIMARY", "SUPP"):
            # Prefer the open (entry=0) comment; keep first strong tag otherwise.
            if int(o.get("entry") or 0) == 0 or pid not in kind_by_pos:
                kind_by_pos[pid] = k
    for o in orders:
        pid = str(o.get("position_id") or "")
        if pid and pid in kind_by_pos:
            o["kind"] = kind_by_pos[pid]
        elif trade_kind(str(o.get("comment") or "")) == "OTHER":
            # Non-strategy / balance noise — drop from desk feeds later.
            o["kind"] = "OTHER"
        comment_l = str(o.get("comment") or "").lower()
        if int(o.get("entry") or 0) == 1:
            if "[sl" in comment_l:
                o["deal_role"] = "SL_EXIT"
            elif "onyxion-exit" in comment_l:
                o["deal_role"] = "ORANGE_EXIT"
            else:
                o["deal_role"] = "EXIT"
        else:
            o["deal_role"] = "OPEN"
    return orders


def _heiken_ashi(
    o: list[float], h: list[float], l: list[float], c: list[float]
) -> tuple[list[float], list[float], list[float], list[float]]:
    n = len(c)
    ha_o = [0.0] * n
    ha_c = [0.0] * n
    ha_h = [0.0] * n
    ha_l = [0.0] * n
    for i in range(n):
        ha_c[i] = (o[i] + h[i] + l[i] + c[i]) / 4.0
        if i == 0:
            ha_o[i] = (o[i] + c[i]) / 2.0
        else:
            ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2.0
        ha_h[i] = max(h[i], ha_o[i], ha_c[i])
        ha_l[i] = min(l[i], ha_o[i], ha_c[i])
    return ha_o, ha_c, ha_h, ha_l


def _wilder_atr(hah: list[float], hac: list[float], hal: list[float], period: int = 6) -> list[float]:
    n = len(hac)
    atr = [0.0] * n
    alpha = 1.0 / period
    sumtr = 0.0
    for i in range(n):
        if i == 0:
            tr = hah[i] - hal[i]
        else:
            tr = max(hah[i] - hal[i], abs(hah[i] - hac[i - 1]), abs(hal[i] - hac[i - 1]))
        if i < period:
            sumtr += tr
            atr[i] = (sumtr / period) if i == period - 1 else tr
        else:
            atr[i] = atr[i - 1] * (1.0 - alpha) + tr * alpha
    return atr


def _supertrend_ha(
    o: list[float],
    h: list[float],
    l: list[float],
    c: list[float],
    period: int = 6,
    mult: float = 0.8,
) -> list[float]:
    """Same SuperTrend-on-HA used by OnyxionDemoBacktest (period 6 / mult 0.8)."""
    _, hac, hah, hal = _heiken_ashi(o, h, l, c)
    atr = _wilder_atr(hah, hac, hal, period)
    n = len(c)
    st = [0.0] * n
    fu = fl = 0.0
    d = 1
    for i in range(n):
        u = hac[i] - mult * atr[i]
        dd = hac[i] + mult * atr[i]
        if i == 0:
            fu, fl, d = u, dd, 1
            st[i] = u
            continue
        fu = max(u, fu) if hac[i - 1] > fu else u
        fl = min(dd, fl) if hac[i - 1] < fl else dd
        if d == 1 and hac[i] < fu:
            d = -1
        elif d == -1 and hac[i] > fl:
            d = 1
        st[i] = fu if d == 1 else fl
    return st


def _sma_at(arr: list[float], i: int, length: int) -> float:
    if i + 1 < length:
        return float("nan")
    return sum(arr[i - length + 1 : i + 1]) / float(length)


def _highest_offset(arr: list[float], i: int, length: int) -> int:
    start = max(0, i - length + 1)
    best = 0
    best_v = float("-inf")
    for off in range(0, i - start + 1):
        v = arr[i - off]
        if v > best_v:
            best_v = v
            best = off
    return best


def _lowest_offset(arr: list[float], i: int, length: int) -> int:
    start = max(0, i - length + 1)
    best = 0
    best_v = float("inf")
    for off in range(0, i - start + 1):
        v = arr[i - off]
        if v < best_v:
            best_v = v
            best = off
    return best


def _gaga_trend(h: list[float], l: list[float], c: list[float]) -> list[float]:
    """KJ GagaTrend (Vintage X-Trend Pine) on the given OHLC series."""
    n = len(c)
    atr = _wilder_atr(h, c, l, 14)
    out = [0.0] * n
    trend = 0
    next_trend = 0
    max_low_price = float(l[0])
    min_high_price = float(h[0])
    up = float("nan")
    down = float("nan")
    prev_trend = trend
    for i in range(n):
        high_price = float(h[i - _highest_offset(h, i, 2)])
        low_price = float(l[i - _lowest_offset(l, i, 3)])
        high_ma = _sma_at(h, i, 2)
        low_fast = _sma_at(l, i, 2)
        low_main = _sma_at(l, i, 3)
        low_slow = _sma_at(l, i, 4)
        prev_low = float(l[i - 1]) if i else float(l[i])
        prev_high = float(h[i - 1]) if i else float(h[i])
        if next_trend == 1:
            max_low_price = max(low_price, max_low_price)
            if high_ma == high_ma and high_ma < max_low_price and c[i] < prev_low:
                trend = 1
                next_trend = 0
                min_high_price = high_price
        else:
            min_high_price = min(high_price, min_high_price)
            bullish_main = (low_main == low_main and low_main > min_high_price) or (
                low_slow == low_slow and low_slow > min_high_price
            )
            bullish_fast = low_fast == low_fast and low_fast > min_high_price
            main_almost = (
                atr[i] == atr[i]
                and low_main == low_main
                and low_main >= (min_high_price - atr[i] * 0.025)
            )
            if (bullish_main or (bullish_fast and main_almost)) and c[i] > prev_high:
                trend = 0
                next_trend = 1
                max_low_price = low_price
        if trend == 0:
            if prev_trend != 0:
                up = down if down == down else max_low_price
            else:
                prev_up = up if up == up else max_low_price
                up = max(max_low_price, prev_up)
        else:
            if prev_trend != 1:
                down = up if up == up else min_high_price
            else:
                prev_down = down if down == down else min_high_price
                down = min(min_high_price, prev_down)
        out[i] = up if trend == 0 else down
        prev_trend = trend
    return out


def _compute_xtrend(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    ha_h: list[float],
    ha_l: list[float],
    ha_c: list[float],
) -> tuple[list[float], str]:
    """Return (xtrend series, source label). Reads XTREND_SOURCE live from env."""
    source = os.environ.get("XTREND_SOURCE", "").strip().lower()
    if not source:
        # Ali desk / bridge: prefer KJ GagaTrend automatically.
        lab = (
            os.environ.get("ASIM_LAB_URL", "")
            + os.environ.get("LAB_URL", "")
            + os.environ.get("DESK_TITLE", "")
            + os.environ.get("MODEL_NAME", "")
        ).lower()
        if "35.223.235.204" in lab or "ali" in lab:
            source = "gaga"
        else:
            source = XTREND_SOURCE or "supertrend"
    if source in ("gaga", "kj", "gagatrend", "vintage"):
        # Match TradingView / CSV Vintage X-Trend: Gaga on the HA signal series.
        return _gaga_trend(ha_h, ha_l, ha_c), "gaga_ha"
    return (
        _supertrend_ha(opens, highs, lows, closes, MT5_XTREND_PERIOD, MT5_XTREND_MULT),
        "supertrend_ha_6_0.8",
    )


def collect_bars(symbol: str, count: int = 96) -> list[dict]:
    # Keep the indicator seed independent of the number of rows displayed.
    # This is exactly 160 closed bars plus the current forming bar, matching
    # OnyxionDemoBacktest.mq5.
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, MT5_CALC_WINDOW + 1)
    if rates is None or len(rates) < 10:
        return []
    opens = [float(r["open"]) for r in rates]
    highs = [float(r["high"]) for r in rates]
    lows = [float(r["low"]) for r in rates]
    closes = [float(r["close"]) for r in rates]
    ha_o, ha_c, ha_h, ha_l = _heiken_ashi(opens, highs, lows, closes)
    rsi = _wilder_rsi(closes, 3)
    ema = _ema_span(rsi, 5)
    xt, xt_source = _compute_xtrend(opens, highs, lows, closes, ha_h, ha_l, ha_c)
    out = []
    start = max(0, len(rates) - count)
    for i in range(start, len(rates)):
        h = float(ema[i] - 50.0)
        # MT5 rate time is the bar open as Unix seconds — same clock MT5 chart shows
        # for Exness (GMT/UTC). Always stamp as UTC ISO so the live desk matches MT5.
        t_unix = int(rates[i]["time"])
        t = datetime.fromtimestamp(t_unix, tz=timezone.utc)
        t_iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(
            {
                "time": t_iso,
                "time_unix": t_unix,
                "time_utc": t_iso,
                "time_tz": "UTC",
                # The live chart and gate audit use the same HA candle that
                # the local Demo engine evaluates. Raw broker OHLC remains
                # available for execution/fill and stop-audit purposes.
                "open": ha_o[i],
                "high": ha_h[i],
                "low": ha_l[i],
                "close": ha_c[i],
                "candle_type": "HA",
                "raw_open": opens[i],
                "raw_high": highs[i],
                "raw_low": lows[i],
                "raw_close": closes[i],
                "signal_open": ha_o[i],
                "signal_high": ha_h[i],
                "signal_low": ha_l[i],
                "signal_close": ha_c[i],
                "forming": i == len(rates) - 1,
                "hist": round(h, 2),
                "histcolor": hist_color(h),
                "xtrend": round(float(xt[i]), 3),
                "xtrend_source": xt_source,
            }
        )
    # Paint the forming M15 candle from the live tick so /live updates between bar closes
    # (and still shows a fresh feed clock when the market is quiet/weekend).
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and out:
            bid = float(getattr(tick, "bid", 0.0) or 0.0)
            ask = float(getattr(tick, "ask", 0.0) or 0.0)
            tick_unix = int(getattr(tick, "time", 0) or 0)
            tick_time = (
                datetime.fromtimestamp(tick_unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if tick_unix
                else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                last = out[-1]
                last["tick_bid"] = round(bid, 3)
                last["tick_ask"] = round(ask, 3)
                last["tick_time"] = tick_time
                last["tick_time_unix"] = tick_unix or None
                if last.get("forming"):
                    last["raw_high"] = max(float(last["raw_high"]), ask, bid)
                    last["raw_low"] = min(float(last["raw_low"]), ask, bid)
                    last["raw_close"] = mid
                    ha_o = float(last["open"])
                    ha_c = (ha_o + float(last["raw_high"]) + float(last["raw_low"]) + mid) / 4.0
                    last["close"] = ha_c
                    last["high"] = max(float(last["raw_high"]), ha_o, ha_c)
                    last["low"] = min(float(last["raw_low"]), ha_o, ha_c)
                    last["signal_high"] = last["high"]
                    last["signal_low"] = last["low"]
                    last["signal_close"] = ha_c
    except Exception:
        pass
    return out


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
        "closed_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_pct": round(100.0 * len(wins) / n, 2) if n else 0.0,
        "net_profit": round(sum(float(t.get("profit") or 0) for t in closed), 2),
        "gross_profit": round(gp, 2),
        "gross_loss": round(gl, 2),
        "profit_factor": (round(gp / gl, 2) if gl > 0 else None),
        "largest_win": round(max((float(t["profit"]) for t in wins), default=0), 2),
        "largest_loss": round(min((float(t["profit"]) for t in losses), default=0), 2),
        "primary_count": len(prim),
        "primary_pnl": round(sum(float(t["profit"]) for t in prim), 2),
        "supp_count": len(supp),
        "supp_pnl": round(sum(float(t["profit"]) for t in supp), 2),
    }


def build_trades_from_deals(raw_deals: list[dict]) -> list[dict]:
    """Pair DEAL_ENTRY_IN / OUT by position_id into PRIMARY/SUPP round-trips."""
    by_pos: dict = {}
    for d in raw_deals:
        pid = str(d.get("position_id") or d.get("order") or d.get("ticket") or "")
        if not pid:
            continue
        by_pos.setdefault(pid, []).append(d)

    trades = []
    for pid, deals in by_pos.items():
        deals = sorted(deals, key=lambda x: str(x.get("time") or ""))
        opens = [d for d in deals if int(d.get("entry") or 0) == 0]
        closes = [d for d in deals if int(d.get("entry") or 0) == 1]
        if not opens:
            continue
        op = opens[0]
        kind = trade_kind(op.get("comment") or "")
        if kind == "OTHER":
            for d in deals:
                k = trade_kind(d.get("comment") or "")
                if k != "OTHER":
                    kind = k
                    break
        # Strategy only opens PRIMARY / SUPP. Skip broker noise (balance, etc.).
        if kind not in ("PRIMARY", "SUPP"):
            continue
        side = op.get("side") or "BUY"
        tr = {
            "position_id": pid,
            "ticket": op.get("ticket"),
            "kind": kind,
            "side": side,
            "volume": op.get("volume"),
            "open_time": op.get("time"),
            "open_price": op.get("price"),
            "open_comment": op.get("comment") or "",
            "magic": op.get("magic"),
            "status": "open",
            "close_time": None,
            "close_price": None,
            "profit": 0.0,
            "sl_comment": "",
        }
        # Chart arrows must use the fill's M15 open, not the prior signal bar.
        ot = _parse_bar_ts(op.get("time"))
        if ot is not None:
            bar_open = int(ot // 900) * 900
            tr["bar_time"] = datetime.fromtimestamp(
                bar_open, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if closes:
            cl = closes[-1]
            close_comment = cl.get("comment") or ""
            close_comment_lower = close_comment.lower()
            tr["status"] = "closed"
            tr["close_time"] = cl.get("time")
            tr["close_price"] = cl.get("price")
            # MT5 deal.profit is already net of commission on most brokers
            tr["profit"] = round(float(cl.get("profit") or 0), 2)
            tr["broker_comment"] = close_comment
            tr["sl_comment"] = close_comment
            if "onyxion-exit" in close_comment_lower:
                tr["exit_reason"] = "ORANGE_HISTOGRAM_EXIT"
            elif "partial-stop" in close_comment_lower:
                tr["exit_reason"] = "SUPPLEMENTARY_STOP"
            elif "[sl" in close_comment_lower:
                tr["exit_reason"] = "BROKER_STOP"
            else:
                tr["exit_reason"] = "MT5_CLOSE"
            tr["ticket_close"] = cl.get("ticket")
        else:
            tr["broker_comment"] = ""
            tr["exit_reason"] = "OPEN"
        trades.append(tr)
    trades.sort(key=lambda t: str(t.get("open_time") or ""))
    return trades


def annotate_bars(bars: list[dict], trades: list[dict] | None = None) -> list[dict]:
    """Explain Demo EA gates per closed M15 bar (prev-body + XT clear + amber arm)."""
    trades = trades or []
    out = []
    seen_amber = False
    run_side = 0  # +1 buy run, -1 sell, 0 none
    for i, b in enumerate(bars):
        prev = bars[i - 1] if i > 0 else None
        hist = float(b.get("hist") or 0)
        zone = "green" if hist >= 10 else ("red" if hist <= -10 else "amber")
        xt = float(b.get("xtrend") or 0) if b.get("xtrend") is not None else None
        o = float(b.get("signal_open", b["open"]))
        h = float(b.get("signal_high", b["high"]))
        l = float(b.get("signal_low", b["low"]))
        c = float(b.get("signal_close", b["close"]))
        body_hi = body_lo = None
        broke_buy = broke_sell = xt_buy = xt_sell = False
        if prev is not None:
            po = float(prev.get("signal_open", prev["open"]))
            pc = float(prev.get("signal_close", prev["close"]))
            body_hi = max(po, pc)
            body_lo = min(po, pc)
            broke_buy = h > body_hi
            broke_sell = l < body_lo
        if xt is not None:
            xt_buy = l > xt
            xt_sell = h < xt

        if zone == "amber":
            seen_amber = True
            run_side = 0

        # Trades that opened on this bar (match open_time to bar window)
        t0 = _parse_bar_ts(b.get("time"))
        t1 = t0 + 15 * 60 if t0 else None
        # Demo EA HourAllowed uses local UTC-4 clock (only when SKIP_WORST_HOURS=1)
        skip_worst = os.environ.get("SKIP_WORST_HOURS", "0").strip().lower() in (
            "1", "true", "yes", "on"
        )
        hour_u4 = None
        hour_blocked = False
        if t0 is not None:
            hour_u4 = int(((t0 // 3600) - 4) % 24)
            hour_blocked = skip_worst and hour_u4 in (0, 3, 5, 17, 18)

        bar_trades = []
        if t0 and t1:
            for tr in trades:
                ot = _parse_bar_ts(tr.get("open_time"))
                if ot is not None and t0 <= ot < t1:
                    bar_trades.append(tr)

        reasons = []
        action = "NONE"
        if bar_trades:
            kinds = ", ".join(f"{t.get('kind')} {t.get('side')}" for t in bar_trades)
            action = kinds
            reasons.append(f"Trade taken: {kinds}")
            for tr in bar_trades:
                if tr.get("kind") == "PRIMARY":
                    run_side = 1 if tr.get("side") == "BUY" else -1
                    seen_amber = False
                elif tr.get("kind") == "SUPP":
                    run_side = 1 if tr.get("side") == "BUY" else -1
        else:
            if hour_blocked:
                reasons.append(f"SKIP: worst hour filter (UTC-4 hour={hour_u4} blocked)")
            elif zone == "amber":
                reasons.append("Amber hist — flatten / arm only; no new primary on amber")
            elif zone == "green":
                if not seen_amber and run_side == 0:
                    reasons.append("SKIP: green hist alone is not enough — need an amber arm first (seenAmber=false)")
                elif run_side == 0 and seen_amber:
                    if not broke_buy:
                        reasons.append(f"SKIP BUY: did not break previous body high ({body_hi})")
                    elif not xt_buy:
                        reasons.append(f"SKIP BUY: candle not clear above X-Trend ({xt})")
                    else:
                        reasons.append(
                            "Body+XT gates OK for PRIMARY BUY but no deal on this bar — "
                            "check MaxPositions / broker reject"
                        )
                elif run_side > 0:
                    if not broke_buy:
                        reasons.append(f"SKIP SUPP BUY: did not break previous body high ({body_hi})")
                    elif not xt_buy:
                        reasons.append(f"SKIP SUPP BUY: not clear above X-Trend ({xt})")
                    else:
                        reasons.append(
                            "Body+XT OK for SUPP BUY but no deal — likely broker rejected SL/order on that tick "
                            "(same gates later allowed SUPP on a following bar)"
                        )
                else:
                    reasons.append("SKIP: run is short-side; green does not open longs mid short-run")
            elif zone == "red":
                if not seen_amber and run_side == 0:
                    reasons.append("SKIP: red hist alone is not enough — need amber arm first")
                elif run_side == 0 and seen_amber:
                    if not broke_sell:
                        reasons.append(f"SKIP SELL: did not break previous body low ({body_lo})")
                    elif not xt_sell:
                        reasons.append(f"SKIP SELL: candle not clear below X-Trend ({xt})")
                    else:
                        reasons.append("Gates OK for SELL but no deal recorded on this bar")
                elif run_side < 0:
                    if not broke_sell:
                        reasons.append(f"SKIP SUPP SELL: did not break previous body low ({body_lo})")
                    elif not xt_sell:
                        reasons.append(f"SKIP SUPP SELL: not clear below X-Trend ({xt})")
                    else:
                        reasons.append(
                            "Body+XT gates OK for SUPP SELL but no deal on this bar — "
                            "likely MaxSupp / broker reject"
                        )
                else:
                    reasons.append("SKIP: run is long-side; red does not open shorts mid long-run")

        row = dict(b)
        row.update(
            {
                "zone": zone,
                "prev_body_high": body_hi,
                "prev_body_low": body_lo,
                "broke_prev_body_buy": broke_buy,
                "broke_prev_body_sell": broke_sell,
                "xt_clear_buy": xt_buy,
                "xt_clear_sell": xt_sell,
                "seen_amber": seen_amber,
                "run_side": run_side,
                "action": action,
                "why": " | ".join(reasons) if reasons else "—",
                "trades_on_bar": [
                    {
                        "kind": t.get("kind"),
                        "side": t.get("side"),
                        "open_price": t.get("open_price"),
                        "close_price": t.get("close_price"),
                        "profit": t.get("profit"),
                    }
                    for t in bar_trades
                ],
            }
        )
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


def collect_broker_snapshot(cfg: dict, symbol: str, lab: dict | None = None) -> dict:
    """Account + positions + round-trip trades + M15 bars/hist/XT for the live desk."""
    import socket

    info = mt5.account_info()
    terminal = mt5.terminal_info()
    account = {}
    if info is not None:
        account = {
            "login": info.login,
            "server": info.server,
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "margin_free": info.margin_free,
            "profit": info.profit,
            "trade_allowed": getattr(info, "trade_allowed", None),
            "trade_expert": getattr(info, "trade_expert", None),
            # Account permission alone is not enough: MT5's terminal-level
            # Algo Trading switch must also be enabled for order_send().
            "terminal_trade_allowed": getattr(terminal, "trade_allowed", None),
            "leverage": info.leverage,
            "currency": info.currency,
            "trade_mode": info.trade_mode,
            "company": info.company,
            "name": info.name,
        }
    magic = int(cfg["magic"])
    # Local Star Trader EA uses 20260826; AWS DEMO bridge uses 126824.
    magics = {magic, 20260826, 126824, 126823, 126825}
    positions = []
    for p in mt5.positions_get(symbol=symbol) or []:
        positions.append(
            {
                "ticket": p.ticket,
                "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume": p.volume,
                "open_time": datetime.fromtimestamp(
                    int(getattr(p, "time", 0) or 0), tz=timezone.utc
                ).isoformat()
                if getattr(p, "time", 0)
                else None,
                "price_open": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "magic": int(p.magic),
                "symbol": p.symbol,
                "comment": getattr(p, "comment", "") or "",
                "kind": trade_kind(getattr(p, "comment", "") or ""),
            }
        )
    orders = []
    try:
        from datetime import timedelta

        to_dt = datetime.now() + timedelta(days=1)
        fr_dt = to_dt - timedelta(days=14)
        deals = mt5.history_deals_get(fr_dt, to_dt) or []
        for d in deals:
            dmagic = int(getattr(d, "magic", 0) or 0)
            dsym = getattr(d, "symbol", "") or ""
            # Include all deals on the gold symbol (don't drop by magic)
            if dsym and dsym != symbol:
                continue
            if not dsym:
                continue
            side = "BUY" if d.type in (0,) or d.type == getattr(mt5, "DEAL_TYPE_BUY", 0) else "SELL"
            comment = d.comment or ""
            orders.append(
                {
                    "time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                    "type": "DEAL",
                    "side": side,
                    "volume": d.volume,
                    "price": d.price,
                    "ticket": d.ticket,
                    "order": d.order,
                    "position_id": int(getattr(d, "position_id", 0) or 0),
                    "comment": comment,
                    "profit": d.profit,
                    "commission": getattr(d, "commission", 0) or 0,
                    "magic": dmagic,
                    "kind": trade_kind(comment),
                    "entry": int(getattr(d, "entry", 0) or 0),
                }
            )
        # Prefer magic-tagged deals, else all symbol deals
        ours = [o for o in orders if o["magic"] in magics]
        orders = enrich_deal_kinds(ours or orders)[-400:]
        # Desk raw-deals feed: only PRIMARY/SUPP strategy legs (open + exit).
        orders = [o for o in orders if o.get("kind") in ("PRIMARY", "SUPP")]
    except Exception as e:
        orders = [{"time": "", "type": "ERROR", "side": "", "volume": 0, "price": 0, "ticket": "", "comment": str(e), "profit": 0, "kind": "OTHER", "entry": 0, "position_id": 0}]

    trades = build_trades_from_deals(orders)
    bars = annotate_bars(collect_bars(symbol, 120), trades)
    decision_records = (
        (cfg.get("_mt5_runtime") or {}).get("decision_records") or []
    )
    decisions_by_bar = {
        str(record.get("bar_time")): record
        for record in decision_records
        if record.get("bar_time")
    }
    for bar in bars:
        decision = decisions_by_bar.get(str(bar.get("time")))
        if decision:
            bar["action"] = decision.get("action") or "NONE"
            bar["filled_action"] = decision.get("filled_action")
            bar["decision_hist"] = decision.get("hist")
            bar["decision_histcolor"] = decision.get("histcolor")
            bar["decision_zone"] = decision.get("zone")
            bar["decision_reason"] = decision.get("decision_reason") or decision.get("why")
            if decision.get("why") or decision.get("decision_reason"):
                bar["why"] = decision.get("why") or decision.get("decision_reason")
            if decision.get("sl") is not None:
                bar["sl"] = decision.get("sl")
            elif decision.get("sl_updated") is not None:
                bar["sl"] = decision.get("sl_updated")
    analysis = analyze_trades(trades)
    last_bar = bars[-1] if bars else {}
    live = cfg.get("_mt5_live_result") or {}
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_unix = int(datetime.now(timezone.utc).timestamp())
    tick_bid = last_bar.get("tick_bid")
    tick_ask = last_bar.get("tick_ask")
    tick_time = last_bar.get("tick_time")
    tick_time_unix = last_bar.get("tick_time_unix")
    if tick_bid is None or tick_ask is None or tick_time is None:
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None:
                tick_bid = round(float(getattr(tick, "bid", 0.0) or 0.0), 3) or None
                tick_ask = round(float(getattr(tick, "ask", 0.0) or 0.0), 3) or None
                tick_unix = int(getattr(tick, "time", 0) or 0)
                if tick_unix:
                    tick_time_unix = tick_unix
                    tick_time = datetime.fromtimestamp(tick_unix, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                else:
                    tick_time = now_iso
                    tick_time_unix = now_unix
        except Exception:
            tick_time = tick_time or now_iso
            tick_time_unix = tick_time_unix or now_unix
    sl_trail = []
    # Historical trail from closed-bar decisions (for TradingView SL line).
    for record in (cfg.get("_mt5_runtime") or {}).get("decision_records") or []:
        sl_val = record.get("sl_updated", record.get("sl"))
        if sl_val is None or not record.get("bar_time"):
            continue
        sl_trail.append(
            {
                "time": record.get("bar_time"),
                "ticket": None,
                "side": record.get("filled_action") or record.get("action"),
                "volume": None,
                "price_open": None,
                "sl": sl_val,
                "tp": None,
                "profit": None,
                "kind": "ENGINE",
                "source": "decision",
                "sl_changed": True,
            }
        )
    for p in positions:
        sl_trail.append(
            {
                "time": now_iso,
                "ticket": p.get("ticket"),
                "side": p.get("side"),
                "volume": p.get("volume"),
                "price_open": p.get("price_open"),
                "sl": p.get("sl"),
                "tp": p.get("tp"),
                "profit": p.get("profit"),
                "kind": p.get("kind"),
                "source": "position",
            }
        )
    if live.get("sl_updated") is not None:
        sl_trail.append(
            {
                "time": now_iso,
                "ticket": None,
                "side": live.get("filled_action") or live.get("action"),
                "volume": None,
                "price_open": None,
                "sl": live.get("sl_updated"),
                "tp": None,
                "profit": None,
                "kind": "ENGINE",
                "source": "engine",
                "sl_changed": bool(live.get("sl_changed")),
            }
        )
    ohlc_records = []
    for bar in bars[-120:]:
        ohlc_records.append(
            {
                "time": bar.get("time"),
                "raw_open": bar.get("raw_open"),
                "raw_high": bar.get("raw_high"),
                "raw_low": bar.get("raw_low"),
                "raw_close": bar.get("raw_close"),
                "ha_open": bar.get("open"),
                "ha_high": bar.get("high"),
                "ha_low": bar.get("low"),
                "ha_close": bar.get("close"),
                "xtrend": bar.get("xtrend"),
                "hist": bar.get("hist"),
                "zone": bar.get("zone") or bar.get("histcolor"),
                "action": bar.get("action"),
                "sl": (live.get("sl_updated") if bars and bar is bars[-1] else bar.get("sl")),
                "forming": bar.get("forming"),
            }
        )

    return {
        "account": account,
        "positions": positions,
        "orders": orders,
        "trades": trades,
        "bars": bars,
        "analysis": analysis,
        "sl_trail": sl_trail,
        "ohlc_records": ohlc_records,
        "symbol": symbol,
        "tick_bid": tick_bid,
        "tick_ask": tick_ask,
        "tick_time": tick_time,
        "tick_time_unix": tick_time_unix,
        "feed_updated_at": now_iso,
        "values_updated_at": now_iso,
        "server_time_utc": now_iso,
        "time_tz": "UTC",
        "bridge": {
            "host": socket.gethostname(),
            "poll_seconds": cfg.get("poll"),
            "model": cfg.get("model"),
            "magic": magic,
            "expected_login": cfg.get("login"),
            "lot": cfg.get("lot"),
            "live_controls": cfg.get("_live_controls") or {},
            "execution_source": cfg.get("execution_source") or "aws_decisions",
            "execution_mode": "MT5 BARS" if cfg.get("execution_source") == "mt5_bars" else "AWS DECISIONS",
            "mt5_connection": "ONLINE",
            "connection_error": None,
            "last_closed_bar": cfg.get("_mt5_last_closed_time"),
            "engine_sl": live.get("sl_updated"),
            "engine_sl_changed": live.get("sl_changed"),
            "tick_bid": tick_bid,
            "tick_ask": tick_ask,
            "tick_time": tick_time,
            "tick_time_unix": tick_time_unix,
            "feed_updated_at": now_iso,
            "server_time_utc": now_iso,
            "time_tz": "UTC",
            "recorded_value_fields": "HA_OHLC,RAW_OHLC,HIST,X_TREND,SL,TICK",
            "recorded_value_count": len(
                (cfg.get("_mt5_runtime") or {}).get("bar_records") or []
            ),
            "recorded_decision_count": len(
                (cfg.get("_mt5_runtime") or {}).get("decision_records") or []
            ),
            "decision_records": (
                (cfg.get("_mt5_runtime") or {}).get("decision_records") or []
            )[-512:],
            "pending_bar_time": (cfg.get("_mt5_runtime") or {}).get("pending_bar_time"),
            "pending_action": (
                (cfg.get("_mt5_runtime") or {}).get("pending_result") or {}
            ).get("action"),
            "engine_action": (
                (cfg.get("_mt5_live_result") or {}).get("filled_action")
                or (cfg.get("_mt5_live_result") or {}).get("action")
            ),
            "engine_reason": (cfg.get("_mt5_live_result") or {}).get("why"),
            "xtrend_source": (bars[-1].get("xtrend_source") if bars else None)
            or os.environ.get("XTREND_SOURCE")
            or XTREND_SOURCE,
        },
        "xtrend_source": (bars[-1].get("xtrend_source") if bars else None)
        or os.environ.get("XTREND_SOURCE")
        or XTREND_SOURCE,
        "lab_action": None if not lab else lab.get("action"),
        "bar_time": last_bar.get("time") if not lab else (lab.get("bar_time") or last_bar.get("time")),
        "bar_time_unix": last_bar.get("time_unix"),
        "tick_time": tick_time,
        "tick_time_unix": tick_time_unix,
        "server_time_utc": now_iso,
        "server_time_unix": now_unix,
        "time_tz": "UTC",
        "note": "ok",
    }


def offline_broker_snapshot(cfg: dict, reason: str) -> dict:
    """Keep the AWS live desk informed while the MT5 terminal is unavailable."""
    import socket

    return {
        "account": {},
        "positions": [],
        "orders": [],
        "trades": [],
        "bars": [],
        "analysis": {},
        "symbol": cfg.get("symbol"),
        "bridge": {
            "host": socket.gethostname(),
            "poll_seconds": cfg.get("poll"),
            "model": cfg.get("model"),
            "magic": int(cfg.get("magic") or 0),
            "expected_login": cfg.get("login"),
            "execution_source": cfg.get("execution_source") or "aws_decisions",
            "execution_mode": "MT5 BARS" if cfg.get("execution_source") == "mt5_bars" else "AWS DECISIONS",
            "mt5_connection": "OFFLINE",
            "connection_error": str(reason)[:300],
            "last_closed_bar": cfg.get("_mt5_last_closed_time"),
            "recorded_value_fields": "HA_OHLC,RAW_OHLC,HIST,X_TREND",
            "recorded_value_count": len(
                (cfg.get("_mt5_runtime") or {}).get("bar_records") or []
            ),
            "recorded_decision_count": len(
                (cfg.get("_mt5_runtime") or {}).get("decision_records") or []
            ),
            "decision_records": (
                (cfg.get("_mt5_runtime") or {}).get("decision_records") or []
            )[-512:],
            "pending_bar_time": (cfg.get("_mt5_runtime") or {}).get("pending_bar_time"),
            "pending_action": (
                (cfg.get("_mt5_runtime") or {}).get("pending_result") or {}
            ).get("action"),
            "engine_action": None,
            "engine_reason": str(reason)[:300],
        },
        "lab_action": None,
        "bar_time": None,
        "note": "bridge online; MT5 terminal unavailable",
    }


def push_broker_heartbeat(cfg: dict, symbol: str, lab: dict | None = None) -> None:
    if cfg.get("model") not in ("ASIM", "DEMO", "TRADINGVIEW_DEMO"):
        return
    try:
        try:
            payload = collect_broker_snapshot(cfg, symbol, lab)
        except Exception as exc:
            payload = offline_broker_snapshot(cfg, str(exc))
        url = cfg["lab_url"].rstrip("/") + "/api/broker/heartbeat"
        http_post_json(url, payload, timeout=20)
    except Exception as e:
        log(f"heartbeat push failed: {e}")


def push_broker_execution(
    cfg: dict,
    symbol: str,
    lab: dict | None,
    *,
    ok: bool,
    results: list,
    note: str = "",
    had_broker_orders: bool = False,
) -> None:
    """Report permanent bar consume to the model's live desk.

    Call only after filled=True / bar consumed — not on failed retries.
    """
    if cfg.get("model") not in ("ASIM", "DEMO", "TRADINGVIEW_DEMO"):
        return
    try:
        snap = collect_broker_snapshot(cfg, symbol, lab)
        payload = {
            "bar_time": None if not lab else lab.get("bar_time"),
            "action": None if not lab else lab.get("action"),
            "ok": bool(ok),
            "results": list(results or []),
            "account": snap.get("account") or {},
            "positions": snap.get("positions") or [],
            "symbol": symbol,
            "note": note or "",
            "had_broker_orders": bool(had_broker_orders),
            "bridge": snap.get("bridge") or {},
        }
        url = cfg["lab_url"].rstrip("/") + "/api/broker/execution"
        resp = http_post_json(url, payload, timeout=30)
        log(
            f"execution push bar={payload.get('bar_time')} ok={ok} "
            f"n_results={len(payload['results'])} matched={resp.get('matched')} "
            f"mt5_status={resp.get('mt5_status')}"
        )
    except Exception as e:
        log(f"execution push failed: {e}")


def _tradable(name: str) -> bool:
    """A symbol the account may actually open a position on.

    Vantage ships several gold symbols per group and only one is enabled: on
    account 25989834 the Forex Major XAUUSD.crp quotes normally but reports
    SYMBOL_TRADE_MODE_DISABLED, while the Gold+ XAUUSD+ is the tradable one.
    The old scan returned the first name containing XAU and USD, so it picked
    the disabled one and every order would have failed at order_send time with
    a market-closed style error that looks nothing like a config mistake.
    """
    info = mt5.symbol_info(name)
    return info is not None and info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL


def resolve_symbol(preferred: str) -> str | None:
    if _tradable(preferred):
        mt5.symbol_select(preferred, True)
        return preferred
    # Common broker aliases, tradable ones only. The preferred symbol is
    # XAUUSDm for the current Exness demo account.
    for name in (preferred, "XAUUSDm", "XAUUSD+", "XAUUSD", "XAUUSD.a", "XAUUSD.", "GOLD"):
        if _tradable(name):
            mt5.symbol_select(name, True)
            return name
    # scan: prefer a tradable match, never return a disabled one
    for s in mt5.symbols_get() or []:
        n = s.name.upper()
        if "XAU" in n and "USD" in n and _tradable(s.name):
            mt5.symbol_select(s.name, True)
            return s.name
    return None


def positions_for_magic(symbol: str, magic: int) -> list:
    pos = mt5.positions_get(symbol=symbol) or []
    return [p for p in pos if int(p.magic) == magic]


def latest_lab_signal(lab_url: str) -> dict:
    state = http_json(lab_url + "api/lab/state")
    decisions = state.get("decisions") or []
    if not decisions:
        return {
            "action": "NONE",
            "bar_time": None,
            "received_at": None,
            "model_name": state.get("model_name"),
        }
    d = decisions[-1]
    action = str(d.get("model_raw_action") or d.get("signal") or "NONE").upper()
    return {
        "action": action,
        "bar_time": d.get("bar_time"),
        "received_at": d.get("received_at"),
        "signal": d.get("signal"),
        "model_name": d.get("model_name") or state.get("model_name"),
        "close": d.get("close") or d.get("mark_price"),
        "broker_orders": d.get("broker_orders") or [],
        "broker_plan": d.get("broker_plan") or {},
    }


def load_state(model: str) -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"{model.lower()}_bridge_state.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"last_bar_time": None, "last_action": None}


def save_state(model: str, st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"{model.lower()}_bridge_state.json"
    p.write_text(json.dumps(st, indent=2), encoding="utf-8")


def save_local_engine(
    engine: Mt5LiveEngine, runtime: dict, model: str = "DEMO"
) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "execution_source": "mt5_bars",
        "model": str(model).upper(),
        "engine": engine.snapshot(),
        "runtime": runtime,
    }
    state_file = _local_engine_state_file(model)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, state_file)


def load_local_engine(
    bars: list[dict], balance: float, model: str = "DEMO"
) -> tuple[Mt5LiveEngine, dict]:
    engine = Mt5LiveEngine()
    state_file = _local_engine_state_file(model)
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if data.get("execution_source") not in (None, "mt5_bars"):
                raise ValueError("state belongs to a different executor")
            if data.get("model") not in (None, str(model).upper()):
                raise ValueError("state belongs to a different model namespace")
            engine.restore(data.get("engine") or {})
            runtime = data.get("runtime") or {}
            runtime.setdefault("last_closed_time", engine.last_closed_time)
            runtime.setdefault("pending_bar_time", None)
            runtime.setdefault("pending_orders", [])
            runtime.setdefault("completed_orders", [])
            runtime.setdefault("entry_sent_bars", [])
            runtime.setdefault("bar_records", [])
            runtime.setdefault("decision_records", [])
            return engine, runtime
        except Exception as exc:
            log(f"local MT5 engine state invalid — rebuilding: {exc}")

    closed = [b for b in bars if not b.get("forming")]
    engine.warmup(closed, balance=balance)
    last = closed[-1].get("time") if closed else None
    runtime = {
        "last_closed_time": last,
        "pending_bar_time": None,
        "pending_orders": [],
        "completed_orders": [],
        "entry_sent_bars": [],
        "last_result": None,
        "bar_records": [],
        "decision_records": [],
    }
    save_local_engine(engine, runtime, model)
    log(f"Local MT5 engine warmed without orders through bar={last}")
    return engine, runtime


def record_mt5_values(runtime: dict, bars: list[dict]) -> None:
    """Append exact displayed HA/raw/indicator values for every MT5 bar."""
    existing = {
        str(record.get("time")): record
        for record in (runtime.get("bar_records") or [])
        if record.get("time")
    }
    for bar in bars:
        record = {
            key: bar.get(key)
            for key in (
                "time",
                "open",
                "high",
                "low",
                "close",
                "raw_open",
                "raw_high",
                "raw_low",
                "raw_close",
                "candle_type",
                "hist",
                "histcolor",
                "xtrend",
                "forming",
                "action",
                "why",
            )
        }
        if record.get("time"):
            existing[str(record["time"])] = record
    runtime["bar_records"] = sorted(
        existing.values(), key=lambda record: str(record.get("time") or "")
    )[-MAX_LOCAL_BAR_RECORDS:]


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
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_order_types() -> set[str]:
    return {"ORDER_TYPE_BUY", "ORDER_TYPE_SELL"}


def _strip_entry_orders(orders: list[dict] | None) -> list[dict]:
    return [
        order
        for order in (orders or [])
        if str(order.get("actionType") or "").upper() not in _entry_order_types()
    ]


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
    if not key:
        return False
    sent = {normalize_bar_time(x) for x in (runtime.get("entry_sent_bars") or [])}
    return key in sent


def _entry_bar_is_stale(bar_time) -> bool:
    """True when a bar closed long ago (catch-up replay).

    Replaying old candles must still trail stops and run the amber flatten, but
    it must never open a position on an hours-old signal at today's price.
    """
    opened = _parse_bar_ts(bar_time)
    if opened is None:
        return False
    age = datetime.now(timezone.utc).timestamp() - (opened + 15 * 60)
    return age > STALE_ENTRY_SECONDS


def _deploy_skip_path() -> Path:
    return STATE_DIR / "deploy_skip_bar.json"


def read_deploy_skip_bar() -> dict | None:
    """Candle the auto-updater asked us to skip for NEW entries only."""
    path = _deploy_skip_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clear_deploy_skip_bar() -> None:
    path = _deploy_skip_path()
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def _should_skip_entries_for_deploy(bar_time) -> bool:
    """True = no new BUY/SELL/SUPP on this bar after a live code update.

    Never flattens or closes positions — trail / amber / broker stops still run.
    """
    data = read_deploy_skip_bar()
    if not data:
        return False
    skip_key = normalize_bar_time(data.get("skip_bar_time"))
    bar_key = normalize_bar_time(bar_time)
    if not skip_key or not bar_key:
        return False
    if bar_key == skip_key:
        return True
    # Past the skip candle — drop the marker so it cannot linger.
    if bar_key > skip_key:
        clear_deploy_skip_bar()
    return False


def engine_order_hints(
    result: dict, symbol: str, *, allow_entries: bool = True
) -> list[dict]:
    """Translate the local Demo engine result into MT5 broker instructions."""
    orders: list[dict] = []
    vol = float(result.get("fill_lot") or 0.01)
    sl = result.get("sl")
    fill_sl = result.get("fill_sl")
    active_sl = result.get("sl_updated")
    broker_sl = active_sl if active_sl is not None else (fill_sl if fill_sl is not None else sl)
    filled = result.get("filled_action") or (
        result.get("action")
        if result.get("action") in ("BUY", "SELL", "BUY_ADD", "SELL_ADD")
        else None
    )
    # Never re-fire market entries from a cached duplicate_bar result — that
    # caused many fills on one M15 when polls reused filled_action.
    if (
        allow_entries
        and not result.get("duplicate_bar")
        and filled in ("BUY", "BUY_ADD", "SELL", "SELL_ADD")
    ):
        orders.append(
            {
                "actionType": "ORDER_TYPE_BUY" if "BUY" in filled else "ORDER_TYPE_SELL",
                "symbol": symbol,
                "volume": vol,
                "sl": broker_sl,
                "stopLoss": broker_sl,
                "kind": "PRIMARY" if filled in ("BUY", "SELL") else "SUPP",
                "engine_action": filled,
            }
        )

    if result.get("action") == "EXIT" or (
        result.get("sl_exits") and result.get("n_total", 1) == 0
    ):
        orders.append({"actionType": "POSITIONS_CLOSE_SYMBOL", "symbol": symbol})
    else:
        closed = []
        if result.get("closed_primary"):
            closed.append(result["closed_primary"])
        closed.extend(result.get("closed_supps") or [])
        for item in closed:
            orders.append(
                {
                    "actionType": "POSITIONS_CLOSE_PARTIAL_SYMBOL",
                    "symbol": symbol,
                    "volume": float(item.get("lot") or vol),
                    "entry": item.get("entry"),
                    "exit": item.get("exit"),
                    "reason": item.get("reason"),
                }
            )

    if result.get("sl_changed") and active_sl is not None and result.get("n_total", 0) > 0:
        orders.append(
            {
                "actionType": "SL_MODIFY",
                "symbol": symbol,
                "sl": active_sl,
                "stopLoss": active_sl,
                "positions": result.get("open_positions") or [],
            }
        )
    return orders


def execute_local_orders(
    cfg: dict, symbol: str, runtime: dict
) -> tuple[bool, list[dict]]:
    results: list[dict] = []
    completed = set(str(x) for x in runtime.get("completed_orders") or [])
    bar_time = runtime.get("pending_bar_time")
    for index, order in enumerate(runtime.get("pending_orders") or []):
        key = str(index)
        if key in completed:
            continue
        action_type = str(order.get("actionType") or "").upper()
        if action_type in _entry_order_types() and _entry_already_sent(runtime, bar_time):
            log(
                f"  skip duplicate entry for bar={normalize_bar_time(bar_time)} "
                f"action={order.get('engine_action')}"
            )
            completed.add(key)
            runtime["completed_orders"] = sorted(completed)
            results.append(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": "entry_already_sent_for_bar",
                    "bar_time": normalize_bar_time(bar_time),
                }
            )
            continue
        ok, one = execute_broker_order(cfg, symbol, order)
        results.extend(one)
        if not ok:
            runtime["completed_orders"] = sorted(completed)
            return False, results
        completed.add(key)
        runtime["completed_orders"] = sorted(completed)
        if action_type in _entry_order_types():
            _mark_entry_sent(runtime, bar_time)
        runtime["_last_order_results"] = results
    runtime["completed_orders"] = sorted(completed)
    return True, results


def process_local_mt5_bar(
    cfg: dict, symbol: str, engine: Mt5LiveEngine, runtime: dict, bar: dict
) -> bool:
    """Evaluate one newly closed MT5 candle and retry its orders safely."""
    bar_key = normalize_bar_time(bar.get("time"))
    pending_key = normalize_bar_time(runtime.get("pending_bar_time"))
    if pending_key and pending_key == bar_key:
        # Drop entry legs already sent for this bar (retry only SL/close leftovers).
        if _entry_already_sent(runtime, bar_key):
            runtime["pending_orders"] = _strip_entry_orders(runtime.get("pending_orders"))
        ok, results = execute_local_orders(cfg, symbol, runtime)
        if not ok:
            attempts = int(runtime.get("pending_attempts") or 0) + 1
            runtime["pending_attempts"] = attempts
            # A leg the broker keeps rejecting must never hold the engine on an old
            # bar: that stops trailing and the amber flatten while a position runs.
            abandon = attempts >= MAX_PENDING_ATTEMPTS
            if abandon:
                runtime["pending_orders"] = []
                runtime["completed_orders"] = []
            save_local_engine(engine, runtime, cfg.get("model", "DEMO"))
            push_broker_execution(
                cfg,
                symbol,
                {
                    "bar_time": bar_key or bar.get("time"),
                    "action": (runtime.get("pending_result") or {}).get("action"),
                },
                ok=False,
                results=results,
                note=(
                    f"pending MT5 bar legs abandoned after {attempts} attempts; engine advanced"
                    if abandon
                    else "pending MT5 bar order failed; retry retained"
                ),
                had_broker_orders=True,
            )
            if not abandon:
                log(
                    f"Pending MT5 bar still not filled bar={bar_key or bar.get('time')} "
                    f"attempt={attempts}/{MAX_PENDING_ATTEMPTS}"
                )
                return False
            log(
                f"Abandoning stuck pending legs bar={bar_key or bar.get('time')} "
                f"after {attempts} attempts — advancing engine to newer bars"
            )
        result = runtime.get("pending_result") or {}
    else:
        tick = mt5.symbol_info_tick(symbol)
        execution_prices = None
        if tick is not None:
            execution_prices = {
                "BUY": float(getattr(tick, "ask", 0.0) or 0.0),
                "SELL": float(getattr(tick, "bid", 0.0) or 0.0),
            }
            execution_prices = {
                side: price for side, price in execution_prices.items() if price > 0
            } or None
        result = engine.push(bar, execution_prices=execution_prices)
        stale_entry = _entry_bar_is_stale(bar.get("time"))
        deploy_skip = _should_skip_entries_for_deploy(bar_key or bar.get("time"))
        allow_entries = (
            not result.get("duplicate_bar")
            and not _entry_already_sent(runtime, bar_key or bar.get("time"))
            and not stale_entry
            and not deploy_skip
        )
        if result.get("duplicate_bar"):
            log(
                f"Duplicate engine bar={bar_key or bar.get('time')} — "
                "skipping market entry re-fire"
            )
        if stale_entry:
            log(
                f"Catch-up bar={bar_key or bar.get('time')} closed too long ago — "
                "state/trail/exits applied, no new entry at current price"
            )
        if deploy_skip:
            log(
                f"DEPLOY SKIP bar={bar_key or bar.get('time')} — "
                "auto-update: no new entries; open positions/SL preserved "
                "(trail/amber/exits still allowed)"
            )
            # Consume the marker once this closed bar has been handled.
            clear_deploy_skip_bar()
            runtime["deploy_skip_bar"] = bar_key or bar.get("time")
            runtime["deploy_skip_logged"] = True
        runtime["pending_bar_time"] = bar_key or bar.get("time")
        runtime["pending_result"] = result
        runtime["pending_orders"] = engine_order_hints(
            result, symbol, allow_entries=allow_entries
        )
        runtime["completed_orders"] = []
        runtime["_last_order_results"] = []
        save_local_engine(engine, runtime, cfg.get("model", "DEMO"))
        ok, results = execute_local_orders(cfg, symbol, runtime)
        if not ok:
            runtime["pending_attempts"] = 1
            save_local_engine(engine, runtime, cfg.get("model", "DEMO"))
            push_broker_execution(
                cfg,
                symbol,
                {
                    "bar_time": bar_key or bar.get("time"),
                    "action": result.get("action"),
                },
                ok=False,
                results=results,
                note="MT5 bar order failed; retry retained",
                had_broker_orders=True,
            )
            log(f"MT5 bar order failed; retrying bar={bar_key or bar.get('time')}")
            return False

    action = result.get("filled_action") or result.get("action") or "NONE"
    log(
        f"MT5 closed bar={bar_key or bar.get('time')} action={action} "
        f"orders={len(runtime.get('pending_orders') or [])} results={len(results)}"
    )
    had_broker_orders = bool(runtime.get("pending_orders") or results)
    runtime["last_closed_time"] = bar_key or bar.get("time")
    runtime["last_result"] = result
    runtime.setdefault("decision_records", [])
    runtime["decision_records"].append(
        {
            "bar_time": bar_key or bar.get("time"),
            "action": result.get("action"),
            "filled_action": result.get("filled_action"),
            "why": result.get("why") or result.get("decision_reason"),
            "decision_reason": result.get("decision_reason"),
            "hist": result.get("hist"),
            "histcolor": result.get("histcolor"),
            "zone": result.get("zone"),
            "ha_side": result.get("ha_side"),
            "position": result.get("position"),
            "n_units": result.get("n_units"),
            "n_supp": result.get("n_supp"),
            "n_total": result.get("n_total"),
            "execution_price_source": result.get("execution_price_source"),
            "fill_price": result.get("fill_price"),
            "fill_sl": result.get("fill_sl"),
            "skip_worst_hours": result.get("skip_worst_hours"),
            "focus_best_hours": result.get("focus_best_hours"),
            "skip_weekends": result.get("skip_weekends"),
            "hour_utc4": result.get("hour_utc4"),
            "xt_skips": result.get("xt_skips"),
            "weekend_skips": result.get("weekend_skips"),
            "sl": result.get("sl_updated", result.get("sl")),
            "sl_updated": result.get("sl_updated"),
            "orders": list(results or []),
        }
    )
    runtime["decision_records"] = runtime["decision_records"][-2048:]
    runtime.pop("pending_bar_time", None)
    runtime.pop("pending_result", None)
    runtime.pop("pending_attempts", None)
    runtime["pending_orders"] = []
    runtime["completed_orders"] = []
    runtime.pop("_last_order_results", None)
    save_local_engine(engine, runtime, cfg.get("model", "DEMO"))
    push_broker_execution(
        cfg,
        symbol,
        {
            "bar_time": bar_key or bar.get("time"),
            "action": result.get("action"),
            "received_at": None,
        },
        ok=True,
        results=results,
        note="local MT5 closed-bar execution",
        had_broker_orders=had_broker_orders,
    )
    return True


def order_send(request: dict) -> dict:
    r = mt5.order_send(request)
    if r is None:
        return {"ok": False, "error": f"order_send None retcode={mt5.last_error()}"}
    ok_codes = {
        mt5.TRADE_RETCODE_DONE,
        getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", -1),
        getattr(mt5, "TRADE_RETCODE_PLACED", -2),
        # 10025 NO_CHANGES: the broker already holds this SL/TP. Treating it as a
        # failure froze the bar pipeline (no trail, no amber flatten) on a stop
        # that was already correct.
        getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025),
    }
    return {
        "ok": r.retcode in ok_codes,
        "retcode": r.retcode,
        "deal": r.deal,
        "order": r.order,
        "price": getattr(r, "price", None),
        "requested_action": request.get("action"),
        "requested_type": request.get("type"),
        "requested_sl": request.get("sl"),
        "requested_position": request.get("position"),
        "requested_volume": request.get("volume"),
        "comment": r.comment,
        "volume": r.volume,
    }


def close_all(symbol: str, magic: int, deviation: int) -> list:
    results = []
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return [{"ok": False, "error": "no tick"}]
    for p in positions_for_magic(symbol, magic):
        if p.type == mt5.POSITION_TYPE_BUY:
            price = tick.bid
            otype = mt5.ORDER_TYPE_SELL
        else:
            price = tick.ask
            otype = mt5.ORDER_TYPE_BUY
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(p.volume),
            "type": otype,
            "position": p.ticket,
            "price": price,
            "deviation": deviation,
            "magic": magic,
            "comment": "onyxion-exit",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode(symbol),
        }
        results.append(order_send(req))
    return results


def close_position(symbol: str, position, volume: float, magic: int, deviation: int) -> dict:
    """Close a specific hedged ticket or part of a netted position."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"ok": False, "error": "no tick"}
    volume = min(float(volume), float(position.volume))
    if volume <= 0:
        return {"ok": True, "noop": True}
    is_buy = position.type == mt5.POSITION_TYPE_BUY
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position": position.ticket,
        "price": tick.bid if is_buy else tick.ask,
        "deviation": deviation,
        "magic": magic,
        "comment": "onyxion-partial-stop",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode(symbol),
    }
    return order_send(req)


def _same_broker_price(symbol: str, a, b) -> bool:
    """True when two prices are identical at the symbol's quoted precision."""
    try:
        left = float(a or 0.0)
        right = float(b or 0.0)
    except (TypeError, ValueError):
        return False
    if left <= 0.0 or right <= 0.0:
        return False
    info = mt5.symbol_info(symbol)
    digits = int(getattr(info, "digits", 2) or 2) if info is not None else 2
    return round(left, digits) == round(right, digits)


def _broker_safe_sl(symbol: str, side: str, sl: float, configured_floor: float = 0.0) -> float:
    """Keep model SL unchanged unless MT5 requires a wider broker-side stop."""
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return float(sl)
    point = float(getattr(info, "point", 0.0) or 0.0)
    stops_level = float(getattr(info, "trade_stops_level", 0.0) or 0.0) * point
    freeze_level = float(getattr(info, "trade_freeze_level", 0.0) or 0.0) * point
    min_distance = max(stops_level, freeze_level, float(configured_floor or 0.0))
    if side == "LONG":
        safe = min(float(sl), float(tick.bid) - min_distance)
    else:
        safe = max(float(sl), float(tick.ask) + min_distance)
    digits = int(getattr(info, "digits", 2) or 2)
    return round(safe, digits)


def modify_position_sl(
    symbol: str, position, sl: float, configured_floor: float = 0.0
) -> dict:
    """Set the broker-side stop for one exact MT5 ticket."""
    side = "LONG" if position.type == mt5.POSITION_TYPE_BUY else "SHORT"
    broker_sl = _broker_safe_sl(symbol, side, sl, configured_floor)
    if _same_broker_price(symbol, getattr(position, "sl", 0.0), broker_sl):
        return {
            "ok": True,
            "retcode": getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025),
            "skipped": True,
            "comment": "SL already at target",
            "requested_sl": broker_sl,
            "requested_position": position.ticket,
        }
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": position.ticket,
        "sl": broker_sl,
        "tp": float(getattr(position, "tp", 0.0) or 0.0),
    }
    return order_send(req)


def filling_mode(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    # prefer IOC then FOK then RETURN
    filling = info.filling_mode
    if filling & 2:  # SYMBOL_FILLING_IOC
        return mt5.ORDER_FILLING_IOC
    if filling & 1:  # FOK
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def open_unit(
    symbol: str,
    side: str,
    lot: float,
    magic: int,
    deviation: int,
    model: str,
    sl: float | None = None,
    configured_floor: float = 0.0,
    kind: str = "",
) -> dict:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"ok": False, "error": "no tick"}
    if side == "LONG":
        otype = mt5.ORDER_TYPE_BUY
        price = tick.ask
    else:
        otype = mt5.ORDER_TYPE_SELL
        price = tick.bid
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": otype,
        "price": price,
        "deviation": deviation,
        "magic": magic,
        "comment": f"onyxion-{model.lower()}-{str(kind or 'OTHER').lower()}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode(symbol),
    }
    if sl is not None:
        req["sl"] = _broker_safe_sl(symbol, side, sl, configured_floor)
    return order_send(req)


def _position_sort_key(position) -> tuple:
    return (
        int(getattr(position, "time_msc", 0) or 0),
        int(getattr(position, "time", 0) or 0),
        int(position.ticket),
    )


def execute_broker_order(cfg: dict, symbol: str, order: dict) -> tuple[bool, list]:
    """Execute one structured instruction. Returns (ok, per-order result dicts)."""
    kind = str(order.get("actionType") or "").upper()
    magic = cfg["magic"]
    deviation = cfg["deviation"]
    positions = sorted(positions_for_magic(symbol, magic), key=_position_sort_key)
    broker_floor = float(
        cfg.get(
            "broker_min_stop_pts",
            DEMO_BROKER_MIN_STOP_PTS
            if cfg.get("model") == "DEMO"
            else ASIM_BROKER_MIN_STOP_PTS,
        )
        or 0.0
    )

    if kind == "POSITIONS_CLOSE_SYMBOL":
        results = close_all(symbol, magic, deviation)
        for r in results:
            log(f"  close-symbol -> {r}")
        return (all(r.get("ok") for r in results) if results else True, results)

    if kind == "POSITIONS_CLOSE_PARTIAL_SYMBOL":
        if not positions:
            return True, []
        entry = order.get("entry")
        position = (
            min(positions, key=lambda p: abs(float(p.price_open) - float(entry)))
            if entry is not None
            else positions[-1]
        )
        result = close_position(
            symbol, position, float(order.get("volume") or cfg["lot"]),
            magic, deviation,
        )
        log(f"  partial #{position.ticket} -> {result}")
        return bool(result.get("ok")), [result]

    if kind in ("ORDER_TYPE_BUY", "ORDER_TYPE_SELL"):
        side = "LONG" if kind == "ORDER_TYPE_BUY" else "SHORT"
        result = open_unit(
            symbol, side, float(order.get("volume") or cfg["lot"]),
            magic, deviation, cfg["model"],
            sl=order.get("sl") if order.get("sl") is not None else order.get("stopLoss"),
            configured_floor=broker_floor,
            kind=order.get("kind") or "",
        )
        log(f"  structured entry -> {result}")
        return bool(result.get("ok")), [result]

    if kind == "SL_MODIFY":
        if not positions:
            return True, []
        desired = order.get("positions") or []
        results = []
        if desired and len(desired) == len(positions):
            for position, target in zip(positions, desired):
                results.append(
                    modify_position_sl(
                        symbol, position, float(target["sl"]), broker_floor
                    )
                )
        else:
            sl = order.get("sl") if order.get("sl") is not None else order.get("stopLoss")
            if sl is None:
                return False, [{"ok": False, "error": "SL_MODIFY missing sl"}]
            results = [
                modify_position_sl(symbol, p, float(sl), broker_floor)
                for p in positions
            ]
        for result in results:
            log(f"  stop modify -> {result}")
        return all(r.get("ok") for r in results), results

    log(f"  reject unknown broker action {kind}")
    return False, [{"ok": False, "error": f"unknown actionType {kind}"}]


def apply_action(cfg: dict, symbol: str, action: str) -> tuple[bool, list]:
    """Apply lab action. Returns (consumable, per-order results)."""
    magic = cfg["magic"]
    lot = cfg["lot"]
    max_units = cfg["max_units"]
    deviation = cfg["deviation"]
    action = (action or "NONE").upper()
    pos = positions_for_magic(symbol, magic)
    n = len(pos)
    side = None
    if n:
        side = "LONG" if pos[0].type == mt5.POSITION_TYPE_BUY else "SHORT"
    out: list = []

    log(f"{cfg['model']} apply {action} | open_units={n} side={side}")

    if action in ("NONE", "HOLD"):
        return True, out

    if action == "EXIT":
        results = close_all(symbol, magic, deviation)
        for r in results:
            log(f"  close -> {r}")
        out.extend(results)
        return True, out

    want = None
    add = False
    if action in ("BUY",):
        want = "LONG"
    elif action in ("SELL",):
        want = "SHORT"
    elif action == "BUY_ADD":
        want, add = "LONG", True
    elif action == "SELL_ADD":
        want, add = "SHORT", True
    else:
        log(f"  ignore unknown action {action}")
        return True, out

    # flip: close opposite first
    if side and side != want:
        for r in close_all(symbol, magic, deviation):
            log(f"  flip-close -> {r}")
            out.append(r)
        n = 0
        side = None

    if add and side == want:
        if n >= max_units:
            log(f"  skip add — at max_units={max_units}")
            return True, out
        r = open_unit(symbol, want, lot, magic, deviation, cfg["model"])
        log(f"  add -> {r}")
        out.append(r)
        return bool(r.get("ok")), out

    # Flat + BUY_ADD/SELL_ADD ⇒ treat as first entry (lab often starts mid scale-in)
    if n == 0 and want:
        r = open_unit(symbol, want, lot, magic, deviation, cfg["model"])
        log(f"  entry -> {r}")
        out.append(r)
        return bool(r.get("ok")), out

    if not add and n > 0 and side == want:
        log("  already in position — hold")
        return True, out

    return True, out


def connect(cfg: dict) -> None:
    path = (cfg.get("terminal") or "").strip() or None
    if path and not Path(path).is_file():
        raise SystemExit(
            f"MT5 terminal path missing: {path}; "
            "install the dedicated Exness terminal before starting the bridge"
        )
    # Pass the credentials loaded from .env during initialization. This lets
    # MT5 switch from a stale terminal account without requiring a manual
    # login, while still preferring the terminal in this Windows session.
    init_kwargs = {
        "login": cfg["login"],
        "password": cfg["password"],
        "server": cfg["server"],
        "timeout": 60000,
    }
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
    if (
        int(getattr(info, "login", 0) or 0) != int(cfg["login"])
        or str(getattr(info, "server", "") or "").strip().casefold()
        != expected_server.casefold()
    ):
        authorized = mt5.login(
            cfg["login"],
            password=cfg["password"],
            server=cfg["server"],
            timeout=60000,
        )
        if not authorized:
            err = mt5.last_error()
            mt5.shutdown()
            raise SystemExit(
                f"MT5 login failed login={cfg['login']} server={cfg['server']}: {err}"
            )
        info = mt5.account_info()
        if info is None:
            mt5.shutdown()
            raise SystemExit("account_info None after account switch")
    if (
        int(getattr(info, "login", 0) or 0) != int(cfg["login"])
        or str(getattr(info, "server", "") or "").strip().casefold()
        != expected_server.casefold()
    ):
        actual = f"{getattr(info, 'login', None)} / {getattr(info, 'server', None)}"
        mt5.shutdown()
        raise SystemExit(
            f"MT5 session mismatch: expected {cfg['login']} / {expected_server}; "
            f"connected {actual}"
        )
    symbol = str(cfg.get("symbol") or "").strip()
    if symbol:
        if not mt5.symbol_select(symbol, True):
            err = mt5.last_error()
            mt5.shutdown()
            raise SystemExit(f"MT5 symbol select failed symbol={symbol}: {err}")
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            mt5.shutdown()
            raise SystemExit(f"MT5 symbol unavailable symbol={symbol}")
        trade_mode = getattr(symbol_info, "trade_mode", None)
        disabled_mode = getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)
        if trade_mode == disabled_mode:
            mt5.shutdown()
            raise SystemExit(f"MT5 symbol trading disabled symbol={symbol}")
    # demo guard
    trade_mode = info.trade_mode  # 0=demo, 1=contest, 2=real
    if trade_mode == 2:
        mt5.shutdown()
        raise SystemExit("REFUSED: account looks REAL (trade_mode=2). Demo only.")
    terminal_info = mt5.terminal_info()
    log(
        f"Connected {cfg['model']} login={info.login} server={info.server} "
        f"balance={info.balance} leverage=1:{info.leverage} mode={trade_mode} "
        f"terminal_trade_allowed={getattr(terminal_info, 'trade_allowed', None)} "
        f"trade_expert={getattr(terminal_info, 'trade_expert', None)} (0=demo)"
    )


def sync_once_mt5_bars(cfg: dict, symbol: str) -> None:
    """Evaluate and execute only newly closed MT5 M15 candles."""
    bars = collect_bars(symbol, count=160)
    closed = [bar for bar in bars if not bar.get("forming")]
    if not closed:
        log("No closed MT5 M15 bar available")
        push_broker_heartbeat(cfg, symbol, None)
        return

    account = mt5.account_info()
    balance = float(getattr(account, "balance", 100.0) or 100.0)
    engine, runtime = load_local_engine(bars, balance, cfg.get("model", "DEMO"))
    # When SKIP_WEEKENDS=1, do not keep retrying a Friday entry over the weekend.
    # Live desks may set SKIP_WEEKENDS=0 to keep evaluating (broker may still reject).
    skip_weekends = os.environ.get("SKIP_WEEKENDS", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if skip_weekends and datetime.now(timezone.utc).weekday() >= 5:
        if runtime.get("pending_bar_time") or runtime.get("pending_orders"):
            engine.pending = None
            runtime["pending_bar_time"] = None
            runtime["pending_result"] = None
            runtime["pending_orders"] = []
            runtime["completed_orders"] = []
            save_local_engine(engine, runtime, cfg.get("model", "DEMO"))
            log("Market closed for weekend; cleared pending MT5 bar order")
        push_broker_heartbeat(cfg, symbol, None)
        return
    record_mt5_values(runtime, bars)
    save_local_engine(engine, runtime, cfg.get("model", "DEMO"))
    cfg["_mt5_runtime"] = runtime
    if not runtime.get("pending_bar_time"):
        # Broker-confirmed realized balance is the risk-gate source of truth.
        engine.engine.balance = balance
    term = mt5.terminal_info()
    trade_ok = bool(term and getattr(term, "trade_allowed", False))

    # Normalize persisted times so Z vs +00:00 cannot replay the same candle.
    if runtime.get("last_closed_time"):
        runtime["last_closed_time"] = normalize_bar_time(runtime.get("last_closed_time"))
    if runtime.get("pending_bar_time"):
        runtime["pending_bar_time"] = normalize_bar_time(runtime.get("pending_bar_time"))
    runtime.setdefault("entry_sent_bars", [])

    pending_time = runtime.get("pending_bar_time")
    last_time = runtime.get("last_closed_time")
    closed_keys = {normalize_bar_time(bar.get("time")) for bar in closed}
    newest_closed = normalize_bar_time(closed[-1].get("time")) if closed else ""
    # Recover broken state: null last_closed must not replay 160 bars.
    if not last_time and newest_closed and not pending_time:
        runtime["last_closed_time"] = newest_closed
        last_time = newest_closed
        save_local_engine(engine, runtime, cfg.get("model", "DEMO"))
        log(f"Initialized empty last_closed_time to newest closed={newest_closed}")
    # Stale / stuck pending: bar left the window, behind last_closed, or a newer
    # bar has already closed while this pending never finished.
    if pending_time:
        pending_key = normalize_bar_time(pending_time)
        newer_exists = bool(newest_closed and pending_key and pending_key < newest_closed)
        if pending_key and (
            pending_key not in closed_keys
            or (last_time and pending_key < normalize_bar_time(last_time))
            or newer_exists
        ):
            log(
                f"Clearing stale pending bar={pending_key} "
                f"last_closed={normalize_bar_time(last_time) or None} "
                f"newest={newest_closed or None}"
            )
            runtime["pending_bar_time"] = None
            runtime["pending_result"] = None
            runtime["pending_orders"] = []
            runtime["completed_orders"] = []
            runtime.pop("pending_attempts", None)
            if not last_time:
                # Jump to newest to avoid mass re-entries after a broken pending.
                runtime["last_closed_time"] = newest_closed or pending_key
            save_local_engine(engine, runtime, cfg.get("model", "DEMO"))
            pending_time = None

    if pending_time:
        pending_key = normalize_bar_time(pending_time)
        pending_bar = next(
            (
                bar
                for bar in closed
                if normalize_bar_time(bar.get("time")) == pending_key
            ),
            {"time": pending_key or pending_time},
        )
        if not trade_ok:
            log("Algo Trading OFF in MT5 — pending local bar will retry")
        elif not process_local_mt5_bar(cfg, symbol, engine, runtime, pending_bar):
            cfg["_mt5_live_result"] = runtime.get("pending_result")
            push_broker_heartbeat(cfg, symbol, None)
            return

    last_time = runtime.get("last_closed_time")
    last_key = normalize_bar_time(last_time) if last_time else ""
    new_bars = [
        bar
        for bar in closed
        if not last_key or normalize_bar_time(bar.get("time")) > last_key
    ]
    if new_bars and not trade_ok:
        log("Algo Trading OFF in MT5 — closed bars remain pending")
    elif trade_ok:
        for bar in new_bars:
            if not process_local_mt5_bar(cfg, symbol, engine, runtime, bar):
                break

    cfg["_mt5_live_result"] = runtime.get("last_result")
    cfg["_mt5_last_closed_time"] = runtime.get("last_closed_time")
    try:
        lab = latest_lab_signal(cfg["lab_url"])
    except Exception as exc:
        log(f"AWS display decision unavailable: {exc}")
        lab = None
    push_broker_heartbeat(cfg, symbol, lab)


def sync_once(cfg: dict) -> None:
    apply_live_controls(cfg)
    symbol = resolve_symbol(cfg["symbol"])
    if not symbol:
        # A closed/disconnected terminal is recoverable; keep the bridge
        # alive and let the main loop reconnect instead of exiting.
        raise RuntimeError(f"Symbol not found for {cfg['symbol']}")
    cfg["symbol_resolved"] = symbol
    info = mt5.symbol_info(symbol)
    log(f"Symbol {symbol} digits={info.digits} point={info.point}")

    if cfg.get("execution_source") == "mt5_bars":
        sync_once_mt5_bars(cfg, symbol)
        return

    lab = latest_lab_signal(cfg["lab_url"])
    st = load_state(cfg["model"])
    bar = lab.get("bar_time")
    action = lab.get("action") or "NONE"
    broker_orders = lab.get("broker_orders") or []
    log(
        f"Lab {cfg['model']} action={action} bar={bar} received={lab.get('received_at')} "
        f"(prev_bar={st.get('last_bar_time')})"
    )

    # trade when a new lab bar arrives — only consume bar if order filled / noop
    if bar and bar != st.get("last_bar_time"):
        term = mt5.terminal_info()
        trade_ok = bool(term and getattr(term, "trade_allowed", False))
        if not trade_ok:
            # Avoid hammering failed order_send every poll while Algo Trading is OFF
            last_warn = st.get("last_algo_warn_at") or ""
            now = datetime.now(timezone.utc)
            warn = True
            if last_warn:
                try:
                    prev = datetime.fromisoformat(last_warn)
                    warn = (now - prev).total_seconds() >= 30
                except Exception:
                    warn = True
            if warn:
                log("Algo Trading OFF in MT5 — cannot trade; will retry when enabled")
                st["last_algo_warn_at"] = now.isoformat()
                save_state(cfg["model"], st)
            else:
                log("Waiting for Algo Trading ON (bar pending)")
        else:
            filled = True
            order_results: list = []
            if broker_orders:
                if st.get("pending_broker_bar") != bar:
                    st["pending_broker_bar"] = bar
                    st["completed_broker_orders"] = []
                    st["pending_broker_results"] = []
                    save_state(cfg["model"], st)
                completed = set(st.get("completed_broker_orders") or [])
                order_results = list(st.get("pending_broker_results") or [])
                for i, order in enumerate(broker_orders):
                    order_key = f"{i}:{json.dumps(order, sort_keys=True, separators=(',', ':'))}"
                    if order_key in completed:
                        continue
                    ok_one, res_one = execute_broker_order(cfg, symbol, order)
                    order_results.extend(res_one)
                    if not ok_one:
                        filled = False
                        st["pending_broker_results"] = order_results
                        save_state(cfg["model"], st)
                        break
                    completed.add(order_key)
                    st["completed_broker_orders"] = sorted(completed)
                    st["pending_broker_results"] = order_results
                    save_state(cfg["model"], st)
            else:
                filled, order_results = apply_action(cfg, symbol, action)
            if filled:
                st["last_bar_time"] = bar
                st["last_action"] = action
                st["last_received_at"] = lab.get("received_at")
                st["updated_at"] = datetime.now(timezone.utc).isoformat()
                st.pop("last_algo_warn_at", None)
                st.pop("last_fail_at", None)
                st.pop("pending_broker_bar", None)
                st.pop("completed_broker_orders", None)
                st.pop("pending_broker_results", None)
                save_state(cfg["model"], st)
                note = (
                    f"structured broker orders ({len(broker_orders)})"
                    if broker_orders
                    else f"apply ({action})"
                )
                log(f"Bar {bar} consumed after {note}")
                # Permanent consume only — Lambda 90s timeout covers stuck pending
                push_broker_execution(
                    cfg,
                    symbol,
                    lab,
                    ok=True,
                    results=order_results,
                    note=note,
                    had_broker_orders=bool(broker_orders),
                )
            else:
                # Back off failed retries (e.g. transient 10027) to every ~30s
                # Do NOT POST /api/broker/execution on retry failures.
                now = datetime.now(timezone.utc)
                last_fail = st.get("last_fail_at")
                do_log = True
                if last_fail:
                    try:
                        do_log = (now - datetime.fromisoformat(last_fail)).total_seconds() >= 30
                    except Exception:
                        do_log = True
                if do_log:
                    log("Trade did not fill — will retry this bar (enable Algo Trading in MT5)")
                    st["last_fail_at"] = now.isoformat()
                    save_state(cfg["model"], st)
    else:
        log("No new lab bar — skip orders")

    pos = positions_for_magic(symbol, cfg["magic"])
    log(f"Open positions ({len(pos)}): " + ", ".join(
        f"#{p.ticket} {'BUY' if p.type==0 else 'SELL'} vol={p.volume}" for p in pos
    ) if pos else "Open positions: none")
    push_broker_heartbeat(cfg, symbol, lab)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--model", required=True,
        choices=[
            "HARD", "SOFT", "ASIM", "DEMO", "TRADINGVIEW_DEMO", "TVDEMO", "TV_DEMO",
            "hard", "soft", "asim", "demo", "tradingview_demo", "tvdemo", "tv_demo",
        ],
    )
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--force-action", default="", help="debug: apply action ignoring lab bar dedupe")
    args = ap.parse_args()
    model = args.model.upper()
    env = load_env(ENV_PATH)
    apply_engine_environment(env)
    cfg = cfg_for(model, env)

    connected = False
    try:
        connect(cfg)
        connected = True
    except SystemExit as exc:
        if "REFUSED" in str(exc) or args.once:
            raise
        log(f"MT5 unavailable at startup — bridge will keep retrying: {exc}")
        try:
            mt5.shutdown()
        except Exception:
            pass
        push_broker_heartbeat(cfg, cfg.get("symbol") or "", None)
    except Exception as exc:
        if args.once:
            raise
        log(f"MT5 unavailable at startup — bridge will keep retrying: {exc}")
        try:
            mt5.shutdown()
        except Exception:
            pass
        push_broker_heartbeat(cfg, cfg.get("symbol") or "", None)

    if cfg.get("execution_source") == "mt5_bars":
        try:
            acquire_local_executor(cfg.get("model", "DEMO"))
        except Exception:
            mt5.shutdown()
            raise
    try:
        if args.force_action:
            if cfg.get("execution_source") == "mt5_bars":
                raise SystemExit(
                    f"REFUSED: {cfg.get('model')} execution is controlled by closed MT5 bars"
                )
            symbol = resolve_symbol(cfg["symbol"])
            ok, results = apply_action(cfg, symbol, args.force_action.upper())
            log(f"force-action done ok={ok} results={results}")
            return
        if args.once:
            sync_once(cfg)
            return
        log(f"Looping every {cfg['poll']}s — Ctrl+C to stop")
        last_reconnect = 0.0
        while True:
            try:
                sync_once(cfg)
                connected = True
            except (Exception, SystemExit) as e:
                connected = False
                log(f"ERROR sync: {e}")
                push_broker_heartbeat(cfg, cfg.get("symbol") or "", None)
                now = time.monotonic()
                if now - last_reconnect >= 10.0:
                    last_reconnect = now
                    try:
                        mt5.shutdown()
                        connect(cfg)
                        connected = True
                        log("MT5 reconnect succeeded")
                    except SystemExit as reconnect_error:
                        if "REFUSED" in str(reconnect_error):
                            raise
                        log(f"MT5 reconnect pending: {reconnect_error}")
                    except Exception as reconnect_error:
                        log(f"MT5 reconnect pending: {reconnect_error}")
            time.sleep(cfg["poll"])
    finally:
        if cfg.get("execution_source") == "mt5_bars":
            release_local_executor()
        mt5.shutdown()
        log("MT5 shutdown")


if __name__ == "__main__":
    main()
