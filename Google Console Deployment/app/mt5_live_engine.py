"""Local Demo/Asim rules engine for closed MT5 candles."""

from __future__ import annotations



import copy

import json

import os

import threading

from collections import OrderedDict

from dataclasses import asdict, dataclass

from datetime import datetime, timezone, timedelta

from pathlib import Path

from typing import List, Optional

API_KEY = os.environ.get('API_KEY', '')

DEFAULT_VOLUME = float(os.environ.get('VOLUME', '0.01'))

SYMBOL = os.environ.get('SYMBOL', 'XAUUSD')

MODEL_LABEL = os.environ.get('MODEL_NAME', 'Demo Trading')

TH = float(os.environ.get('HIST_THRESH', '10'))

LEVERAGE = float(os.environ.get('LEVERAGE', '100'))

MARGIN_MIN = float(os.environ.get('MARGIN_MIN_PCT', '75')) / 100.0

MAXPOS = int(os.environ.get('MAXPOS', '20'))

MAX_SUPP = int(os.environ.get('MAX_SUPP', '10'))

# Playbook A: 0.25-pt trail. Broker min-stop applied only on MT5 SL place/modify.
_TSL_DESIRED = float(os.environ.get('TSL_PTS', '0.25'))

BROKER_MIN_STOP_PTS = float(os.environ.get('BROKER_MIN_STOP_PTS', '0.30'))

TSL_PTS = _TSL_DESIRED

STOP_SLIPPAGE_PTS = float(os.environ.get('STOP_SLIPPAGE_PTS', '0'))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def effective_tsl_pts() -> float:
    """Read TSL live so dashboard controls apply without process restart."""
    return _env_float('TSL_PTS', 0.25)


def effective_trail_every_candle() -> bool:
    return os.environ.get('TRAIL_EVERY_CANDLE', '1').strip().lower() in ('1', 'true', 'yes', 'on')


def effective_entry_bar_mode() -> str:
    mode = os.environ.get('ENTRY_BAR_MODE', 'defer').strip().lower()
    return mode if mode in ('off', 'test', 'defer') else 'defer'


def effective_supp_xt_gate() -> bool:
    """X-Trend gate for SUPP entries. Default OFF: SUPP only needs the trade-wick break."""
    return os.environ.get('XTREND_GATE_SUPP', '0').strip().lower() in ('1', 'true', 'yes', 'on')


def effective_trail_entry_bar() -> bool:
    """Trail the stop to the entry bar's close. Default ON.

    The entry bar is never stop-TESTED (the wick that filled the trade must not
    kill it), but its close must still move the stop. With this OFF the stop
    stays at entry +/- TSL into the next bar, so any retrace closes the trade
    for exactly -(TSL + slippage) - spread.
    """
    return os.environ.get('TRAIL_ENTRY_BAR', '1').strip().lower() in ('1', 'true', 'yes', 'on')


def effective_skip_weekends() -> bool:
    """Live-readable weekend filter (dashboard / .env without restart)."""
    return os.environ.get('SKIP_WEEKENDS', '1').strip().lower() in ('1', 'true', 'yes', 'on')


CONTRACT = 100.0

STOPOUT = 0.5

BEST_LOT_MULT = float(os.environ.get('BEST_LOT_MULT', '1.0'))

SPREAD_COST = float(os.environ.get('SPREAD_COST', '0.06'))

STATE_FILE = Path(os.environ.get('STATE_FILE', '/opt/demo-trading/state.json'))

MAX_SEEN_BARS = 2048

WORST_HOURS_UTC4 = {0, 3, 5, 17, 18}

BEST_HOURS_UTC4 = {2, 9, 10, 11, 19, 21}

HOUR_TZ_OFFSET = -4

START_BALANCE = float(os.environ.get('START_BALANCE', '100'))

SKIP_WORST_HOURS = os.environ.get('SKIP_WORST_HOURS', '0').strip().lower() in ('1', 'true', 'yes', 'on')

FOCUS_BEST_HOURS = os.environ.get('FOCUS_BEST_HOURS', '0').strip().lower() in ('1', 'true', 'yes', 'on')

SKIP_WEEKENDS = os.environ.get('SKIP_WEEKENDS', '1').strip().lower() in ('1', 'true', 'yes', 'on')

XT_GATE = os.environ.get('XTREND_GATE', '1').strip().lower() in ('1', 'true', 'yes', 'on')

# SUPP X-Trend gate is separate and OFF by default (SUPP only needs the trade-wick break).
XT_GATE_SUPP = effective_supp_xt_gate()

XT_BUF = float(os.environ.get('XTREND_BUF', '0'))

PRIMARY_WICK_GATE = os.environ.get('PRIMARY_WICK_GATE', '0').strip().lower() in ('1', 'true', 'yes', 'on')

ENTRY_BAR_MODE = effective_entry_bar_mode()

# Trail every closed candle by default so winners lock in; override with TRAIL_EVERY_CANDLE=0.
TRAIL_EVERY_CANDLE = effective_trail_every_candle()

# When enabled, positions exit only on orange histogram (or margin stop), not broker SL.
DISABLE_STOP_LOSS = os.environ.get('DISABLE_STOP_LOSS', '0').strip().lower() in ('1', 'true', 'yes', 'on')

def body_high(o: float, c: float) -> float:
    return o if o >= c else c

def body_low(o: float, c: float) -> float:
    return c if o >= c else o

def _parse_local_dt_utc4(time_str: Optional[str]) -> Optional[datetime]:
    if not time_str:
        return None
    try:
        ts = str(time_str).strip().replace('Z', '+00:00')
        if 'T' not in ts and ' ' in ts:
            ts = ts.replace(' ', 'T', 1)
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=HOUR_TZ_OFFSET)))
    except Exception:
        return None

def _parse_hour_utc4(time_str: Optional[str]) -> Optional[int]:
    local = _parse_local_dt_utc4(time_str)
    return None if local is None else int(local.hour)

def is_weekend_bar(time_str: Optional[str]) -> bool:
    """True when bar local time (UTC-4) falls on Saturday or Sunday."""
    local = _parse_local_dt_utc4(time_str)
    if local is None:
        return False
    return local.weekday() >= 5

def hour_allowed(hour: Optional[int], skip_worst: bool) -> bool:
    if hour is None:
        return True
    if skip_worst and hour in WORST_HOURS_UTC4:
        return False
    return True

def lot_for_hour(hour: Optional[int], focus_best: bool, base_lot: float) -> float:
    if focus_best and hour is not None and (hour in BEST_HOURS_UTC4):
        return base_lot * BEST_LOT_MULT
    return base_lot

def _auth(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail='bad api key')

def _validate_raw_group(candles: 'List[Candle]') -> None:
    """Reject a partial raw_* group as a 400, not an unhandled 500."""
    for c in candles:
        vals = (c.raw_open, c.raw_high, c.raw_low, c.raw_close)
        if any((v is not None for v in vals)) and (not all((v is not None for v in vals))):
            raise HTTPException(status_code=400, detail='raw_open, raw_high, raw_low and raw_close must be sent together')

def zone(h: float) -> int:
    if h >= TH:
        return 1
    if h <= -TH:
        return -1
    return 0

def zone_from_sent(hist: float, color: Optional[str]) -> tuple[int, str]:
    c = str(color or '').strip().lower()
    if c in ('green', 'g'):
        return (1, 'green')
    if c in ('red', 'r'):
        return (-1, 'red')
    if c in ('orange', 'o', 'amber', 'a'):
        return (0, 'orange')
    zz = zone(float(hist))
    name = 'green' if zz > 0 else 'red' if zz < 0 else 'orange'
    return (zz, name)

def ha_side(o: float, c: float) -> int:
    """+1 green HA (close > open), -1 red HA, 0 doji."""
    if c > o:
        return 1
    if c < o:
        return -1
    return 0


def explain_decision(
    code: str,
    *,
    action: str = "NONE",
    zone: str = "",
    hist: float | None = None,
    seen_amber: bool = False,
    n_total: int = 0,
    xtrend: float | None = None,
    xt_gate: bool = True,
    trail: bool = False,
    tsl: float = 0.25,
) -> str:
    """Human-readable bar reason aligned with Demo/Asim live trading rules."""
    z = str(zone or "").lower()
    hist_s = f"{float(hist):.2f}" if hist is not None else "—"
    xt_s = f"{float(xtrend):.3f}" if xtrend is not None else "—"
    code_u = str(code or "NO_ENTRY_SIGNAL").upper()
    rules = (
        "Rules: amber arm first → green/red must break previous body → "
        f"{'X-Trend must be clear' if xt_gate else 'X-Trend gate OFF'} → "
        f"{'trail every candle at ' + str(tsl) + ' pt' if trail else 'trail-every-candle OFF'} → "
        "amber flattens open positions."
    )
    mapping = {
        "SKIP_WEEKEND_MARKET_CLOSED": "No trade — weekend market closed (skip-weekends rule).",
        "WAITING_FOR_AMBER_ARM": (
            f"No trade — hist {hist_s} ({z or 'colour'}) but engine is not amber-armed yet. "
            "Must see an amber/orange bar after the last primary before the next entry."
        ),
        "AMBER_ARM_OR_FLAT": (
            f"No new entry — amber/orange hist {hist_s} arms the engine and flattens any open run. "
            "Waiting for the next green/red body-break that clears X-Trend."
        ),
        "SKIP_PRIMARY_PREVIOUS_BODY_GATE": (
            f"No primary — hist {hist_s} ({z}) but price did not break the previous candle body "
            "(entry rule: prev_body_cross)."
        ),
        "SKIP_SUPPLEMENTARY_PREVIOUS_BODY_GATE": (
            f"No SUPP — hist {hist_s} ({z}) but price did not break the previous candle body."
        ),
        "SKIP_SUPPLEMENTARY_NO_TRADE_WICK": (
            "No SUPP — no last-trade candle wick level stored yet."
        ),
        "SKIP_SUPPLEMENTARY_TRADE_WICK_GATE": (
            "No SUPP — next candle did not break the wick of the candle where the last trade was taken."
        ),
        "SKIP_PRIMARY_XTREND_GATE": (
            f"No primary — candle touches/crosses X-Trend ({xt_s}). Whole candle must sit clear of XT."
        ),
        "SKIP_SUPPLEMENTARY_XTREND_GATE": (
            f"No SUPP — candle touches/crosses X-Trend ({xt_s}). SUPP XT gate is ON (XTREND_GATE_SUPP=1)."
        ),
        "SKIP_SUPPLEMENTARY_CLOSE_PROGRESS_BUY": (
            "No BUY SUPP — close did not progress above the last trade candle close."
        ),
        "SKIP_SUPPLEMENTARY_CLOSE_PROGRESS_SELL": (
            "No SELL SUPP — close did not progress below the last trade candle close."
        ),
        "REJECTED_LOCAL_RISK_OR_MAXPOS": "No trade — blocked by margin/risk or max open positions.",
        "REJECTED_MAX_SUPPLEMENTARY_POSITIONS": "No SUPP — already at max supplementary positions.",
        "ORANGE_HISTOGRAM_EXIT": "Exit — amber/orange histogram flattens the open run and resets trailing stops.",
        "HOLDING_MANAGE_TRAIL": (
            f"Holding {n_total} unit(s); managing stops"
            f"{' / trailing every candle' if trail else ''}. No new entry on this bar."
        ),
        "NO_ENTRY_SIGNAL": (
            f"No trade — hist {hist_s} ({z or '—'}), amber_armed={seen_amber}. "
            "Primary needs body-break + XT-clear; SUPP needs break of last trade candle wick only."
        ),
        "NO_ACTION": (
            f"No trade — hist {hist_s} ({z or '—'}), amber_armed={seen_amber}. "
            "Primary needs body-break + XT-clear; SUPP needs break of last trade candle wick."
        ),
    }
    if code_u.startswith("STOP_EXIT_"):
        n = code_u.replace("STOP_EXIT_", "")
        text = f"Stop exit — {n} unit(s) hit trailing/protective stop on this bar."
    elif code_u.startswith("ENTRY_FILLED_"):
        side = code_u.replace("ENTRY_FILLED_", "")
        kind = "primary" if side in ("BUY", "SELL") else "supplementary"
        text = (
            f"Trade taken — filled {side} ({kind}). "
            f"Hist {hist_s} ({z}); amber was armed; previous-body break + XT clear passed."
        )
    elif code_u.startswith("SKIP_WORST_HOUR_"):
        hr = code_u.replace("SKIP_WORST_HOUR_", "")
        text = f"No trade — worst-hour filter blocked entries at UTC-4 hour {hr}."
    else:
        text = mapping.get(code_u, f"{code_u.replace('_', ' ').title()} — action={action}.")
    return f"{text} {rules}"


@dataclass
class Position:
    entry: float
    sl: float
    is_primary: bool
    lot: float

@dataclass
class Pending:
    want: int
    is_primary: bool
    signal_break_level: float
    lot: float

class LatestModsEngine:
    """
    seen_amber     amber has appeared since last primary
    run_side       +1 buy primary already taken this run; -1 sell; 0 none
    break_level    last filled trade wick (display only)
    last_trade_close close of the last candle that created a trade
    prev_open/close previous candle body used for the cross and the fill
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.prev_high: float | None = None
        self.prev_low: float | None = None
        self.prev_open: float | None = None
        self.prev_close: float | None = None
        self.pos = 0
        self.positions: list[Position] = []
        self.break_level: float | None = None
        self.last_trade_close: float | None = None
        self.seen_amber = False
        self.run_side = 0
        self.pending: Pending | None = None
        self.warmed = False
        self.balance = START_BALANCE
        self.hist = 0.0
        self.skip_worst_hours = SKIP_WORST_HOURS
        self.focus_best_hours = FOCUS_BEST_HOURS
        self.hour_skips = 0
        self.xt_skips = 0
        self.weekend_skips = 0
        self._last_fill_lot = DEFAULT_VOLUME
        self.last_execution_source = 'not_seen'
        self.last_entry_reason: str | None = None
        self._seen_bars: 'OrderedDict[str, dict]' = OrderedDict()

    def snapshot(self) -> dict:
        """JSON-safe state used to survive service restarts."""
        return {'prev_high': self.prev_high, 'prev_low': self.prev_low, 'prev_open': self.prev_open, 'prev_close': self.prev_close, 'pos': self.pos, 'positions': [asdict(p) for p in self.positions], 'break_level': self.break_level, 'last_trade_close': self.last_trade_close, 'seen_amber': self.seen_amber, 'run_side': self.run_side, 'pending': None if self.pending is None else asdict(self.pending), 'warmed': self.warmed, 'balance': self.balance, 'hist': self.hist, 'skip_worst_hours': self.skip_worst_hours, 'focus_best_hours': self.focus_best_hours, 'hour_skips': self.hour_skips, 'xt_skips': self.xt_skips, 'weekend_skips': self.weekend_skips, 'last_fill_lot': self._last_fill_lot, 'last_execution_source': self.last_execution_source, 'seen_bars': list(self._seen_bars.items())}

    def restore(self, data: dict) -> None:
        """Restore a validated snapshot; callers fall back to reset on error."""
        self.prev_high = data.get('prev_high')
        self.prev_low = data.get('prev_low')
        self.prev_open = data.get('prev_open')
        self.prev_close = data.get('prev_close')
        self.pos = int(data.get('pos') or 0)
        self.positions = [Position(**p) for p in data.get('positions') or []]
        self.break_level = data.get('break_level')
        saved_trade_close = data.get('last_trade_close')
        self.last_trade_close = None if saved_trade_close is None else float(saved_trade_close)
        self.seen_amber = bool(data.get('seen_amber'))
        self.run_side = int(data.get('run_side') or 0)
        pending = data.get('pending')
        self.pending = Pending(**pending) if pending else None
        self.warmed = bool(data.get('warmed'))
        self.balance = float(data.get('balance', START_BALANCE))
        self.hist = float(data.get('hist', 0.0))
        self.skip_worst_hours = SKIP_WORST_HOURS
        self.focus_best_hours = FOCUS_BEST_HOURS
        self.hour_skips = int(data.get('hour_skips') or 0)
        self.xt_skips = int(data.get('xt_skips') or 0)
        self.weekend_skips = int(data.get('weekend_skips') or 0)
        self._last_fill_lot = float(data.get('last_fill_lot', DEFAULT_VOLUME))
        self.last_execution_source = str(data.get('last_execution_source') or 'not_seen')
        self._seen_bars = OrderedDict(data.get('seen_bars') or [])
        while len(self._seen_bars) > MAX_SEEN_BARS:
            self._seen_bars.popitem(last=False)

    def _n_total(self) -> int:
        return len(self.positions)

    def _n_primary(self) -> int:
        return sum((1 for p in self.positions if p.is_primary))

    def _n_supp(self) -> int:
        return sum((1 for p in self.positions if not p.is_primary))

    def _floating(self, px: float) -> float:
        tot = 0.0
        for p in self.positions:
            tot += (px - p.entry if self.pos > 0 else p.entry - px) * CONTRACT * p.lot
        return tot

    def _used(self, px: float) -> float:
        if not self.positions or LEVERAGE <= 0:
            return 0.0
        return sum((p.lot for p in self.positions)) * CONTRACT * px / LEVERAGE

    def _mlevel(self, px: float) -> float:
        u = self._used(px)
        if u <= 0:
            return 999.0
        return (self.balance + self._floating(px)) / u

    def _can_add(self, px: float, lot: float) -> bool:
        if self._n_total() + 1 > MAXPOS:
            return False
        fee = SPREAD_COST
        proj_used = self._used(px) + lot * CONTRACT * px / LEVERAGE
        eq = self.balance + self._floating(px)
        proj_eq = eq - fee
        if proj_used > 0 and proj_eq / proj_used < MARGIN_MIN:
            return False
        if eq - self._used(px) < lot * CONTRACT * px / LEVERAGE + fee:
            return False
        return True

    def _init_sl(self, entry: float, want: int) -> float:
        tsl = effective_tsl_pts()
        if want > 0:
            return entry - tsl
        return entry + tsl

    def _trail_sl(self, sl: float, close: float, side: int) -> float:
        tsl = effective_tsl_pts()
        if side > 0:
            return max(sl, close - tsl)
        return min(sl, close + tsl)

    def _active_sl(self) -> float | None:
        """The stop actually protecting the run, not just the primary's stop.

        The primary is stopped out long before the supplementaries in most
        runs, so reporting only its stop left `sl` null while tickets were
        still live.
        """
        if not self.positions:
            return None
        primary = next((p for p in self.positions if p.is_primary), None)
        if primary is not None:
            return primary.sl
        stops = [p.sl for p in self.positions]
        return max(stops) if self.pos > 0 else min(stops)

    def _clear_all(self) -> None:
        self.positions = []
        self.pos = 0
        self.break_level = None
        self.last_trade_close = None
        self.pending = None

    def _flatten(self, px: float) -> str:
        self.balance += self._floating(px)
        self._clear_all()
        self.pending = None
        return 'EXIT'

    def _close_one(self, p: Position, px: float) -> float:
        pnl = (px - p.entry if self.pos > 0 else p.entry - px) * CONTRACT * p.lot
        self.balance += pnl
        return pnl

    def _do_open(self, fill_px: float, want: int, is_primary: bool, lot: float, signal_bl: float, trade_close: float | None = None) -> str:
        if not self._can_add(fill_px, lot):
            self.last_entry_reason = 'REJECTED_LOCAL_RISK_OR_MAXPOS'
            return 'NONE'
        if is_primary:
            self.pos = want
            self.run_side = want
            self.seen_amber = False
            self.positions = [Position(entry=fill_px, sl=self._init_sl(fill_px, want), is_primary=True, lot=lot)]
            self._last_fill_lot = lot
            self.balance -= SPREAD_COST
            self.break_level = signal_bl
            self.last_trade_close = trade_close
            return 'BUY' if want > 0 else 'SELL'
        if self.pos == 0:
            self.pos = self.run_side if self.run_side else want
        if self._n_supp() >= MAX_SUPP:
            self.last_entry_reason = 'REJECTED_MAX_SUPPLEMENTARY_POSITIONS'
            return 'HOLD'
        side = self.pos
        self.positions.append(Position(entry=fill_px, sl=self._init_sl(fill_px, side), is_primary=False, lot=lot))
        self.break_level = signal_bl
        self.last_trade_close = trade_close
        self._last_fill_lot = lot
        self.balance -= SPREAD_COST
        return 'BUY_ADD' if side > 0 else 'SELL_ADD'

    def _xt_clear(self, h: float, l: float, xt: float, want: int) -> bool:
        # Playbook: whole candle must sit clear of X-Trend (wick on line → skip).
        if want > 0:
            return l > xt + XT_BUF
        return h < xt - XT_BUF

    def _xt_touch(self, h: float, l: float, xt: float) -> bool:
        """True when this candle's range touches / crosses the X-Trend line."""
        return (l - XT_BUF) <= xt <= (h + XT_BUF)

    def _ha_matches(self, o: float, c: float, want: int) -> bool:
        return ha_side(o, c) == want

    def _broke_wick(self, h: float, l: float, level: float, want: int) -> bool:
        if want > 0:
            return h > level
        return l < level

    def _broke_prev_body(self, h: float, l: float, want: int) -> bool:
        if self.prev_open is None or self.prev_close is None:
            return False
        if want > 0:
            return h > body_high(self.prev_open, self.prev_close)
        return l < body_low(self.prev_open, self.prev_close)

    def _cross_fill(self, want: int, eo: float, eh: float | None=None, el: float | None=None) -> float:
        tsl = effective_tsl_pts()
        if want > 0:
            lvl = body_high(self.prev_open, self.prev_close)
            if eo > lvl:
                return eo
            if el is not None and abs(el - eo) < 1e-09 and (eo < lvl - tsl):
                return eo
            return lvl
        lvl = body_low(self.prev_open, self.prev_close)
        if eo < lvl:
            return eo
        if eh is not None and abs(eh - eo) < 1e-09 and (eo > lvl + tsl):
            return eo
        return lvl

    def _wick_fill(self, want: int, eo: float, level: float) -> float:
        """SUPP fill at last-trade wick level (or this open if already through)."""
        if want > 0:
            return eo if eo > level else float(level)
        return eo if eo < level else float(level)

    def _entry_ok(self, o: float, h: float, l: float, c: float, xt: float, want: int, level: float, is_primary: bool=False, trade_close: float | None=None) -> bool:
        # Primary: previous-body break + X-Trend clear.
        # SUPP: only break the wick of the candle where the last trade was taken (no XT gate).
        if is_primary:
            if not self._broke_prev_body(h, l, want):
                self.last_entry_reason = 'SKIP_PRIMARY_PREVIOUS_BODY_GATE'
                return False
            if XT_GATE and (not self._xt_clear(h, l, xt, want)):
                self.xt_skips += 1
                self.last_entry_reason = 'SKIP_PRIMARY_XTREND_GATE'
                return False
            return True
        if level is None:
            self.last_entry_reason = 'SKIP_SUPPLEMENTARY_NO_TRADE_WICK'
            return False
        if not self._broke_wick(h, l, float(level), want):
            self.last_entry_reason = 'SKIP_SUPPLEMENTARY_TRADE_WICK_GATE'
            return False
        # Optional: XTREND_GATE_SUPP=1 also demands the candle sit clear of X-Trend.
        if effective_supp_xt_gate() and (not self._xt_clear(h, l, xt, want)):
            self.xt_skips += 1
            self.last_entry_reason = 'SKIP_SUPPLEMENTARY_XTREND_GATE'
            return False
        return True

    def _open_on_cross(self, want: int, is_primary: bool, lot: float, h: float, l: float, eo: float, trade_close: float | None = None, execution_price: float | None = None) -> str:
        if execution_price is not None:
            fill_px = float(execution_price)
        elif is_primary:
            fill_px = self._cross_fill(want, eo, h, l)
        else:
            if self.break_level is None:
                self.last_entry_reason = 'SKIP_SUPPLEMENTARY_NO_TRADE_WICK'
                return 'NONE'
            fill_px = self._wick_fill(want, eo, float(self.break_level))
        sig_bl = h if want > 0 else l
        return self._do_open(fill_px, want, is_primary, lot, sig_bl, trade_close)

    def _scan_stops(self, eh: float, el: float, eo: float, has_raw_execution: bool, only: list[Position] | None=None):
        sl_exits = 0
        primary_closed = False
        closed_primary: dict | None = None
        closed_supps: list[dict] = []
        if DISABLE_STOP_LOSS or self._n_total() == 0:
            return (sl_exits, primary_closed, closed_primary, closed_supps)
        check_ids = None if only is None else {id(p) for p in only}
        survivors: list[Position] = []
        for p in self.positions:
            if check_ids is not None and id(p) not in check_ids:
                survivors.append(p)
                continue
            hit = self.pos > 0 and el <= p.sl or (self.pos < 0 and eh >= p.sl)
            if not hit:
                survivors.append(p)
                continue
            slipped_stop = p.sl - STOP_SLIPPAGE_PTS if self.pos > 0 else p.sl + STOP_SLIPPAGE_PTS
            # Fill at the slipped stop. Do not take a worse gap-through open:
            # that is what turned a huge entry-bar trail into a fake loss.
            stop_fill = slipped_stop
            pnl = self._close_one(p, stop_fill)
            sl_exits += 1
            rec = {'entry': p.entry, 'exit': stop_fill, 'lot': p.lot, 'pnl': round(pnl, 4), 'reason': 'primary_tsl' if p.is_primary else 'supp_tsl'}
            if p.is_primary:
                primary_closed = True
                closed_primary = rec
            else:
                closed_supps.append(rec)
        self.positions = survivors
        if not self.positions:
            self.pos = 0
        return (sl_exits, primary_closed, closed_primary, closed_supps)

    @staticmethod
    def _entry_execution_price(want: int, execution_prices: dict | None, fallback: float) -> float:
        if not execution_prices:
            return fallback
        key = 'BUY' if want > 0 else 'SELL'
        value = execution_prices.get(key)
        return fallback if value is None else float(value)

    def push(self, o: float, h: float, l: float, c: float, xtrend: float, hist_in: float | None, histcolor: str | None=None, skip_worst_hours: bool | None=None, focus_best_hours: bool | None=None, time_str: str | None=None, raw_open: float | None=None, raw_high: float | None=None, raw_low: float | None=None, raw_close: float | None=None, execution_prices: dict | None=None):
        raw_values = (raw_open, raw_high, raw_low, raw_close)
        if any((v is not None for v in raw_values)) and (not all((v is not None for v in raw_values))):
            raise ValueError('raw_open, raw_high, raw_low and raw_close must be sent together')
        bar_key = str(time_str or '').strip()
        if bar_key and bar_key in self._seen_bars:
            duplicate = copy.deepcopy(self._seen_bars[bar_key])
            duplicate['duplicate_bar'] = True
            duplicate['dedupe_key'] = bar_key
            return duplicate
        if effective_skip_weekends() and is_weekend_bar(time_str):
            self.weekend_skips += 1
            self.warmed = True
            skip = {'action': 'NONE', 'why': explain_decision('SKIP_WEEKEND_MARKET_CLOSED', action='NONE', zone='amber', hist=(0.0 if hist_in is None else float(hist_in)), seen_amber=self.seen_amber, n_total=self._n_total(), xt_gate=XT_GATE, trail=effective_trail_every_candle(), tsl=effective_tsl_pts()), 'decision_reason': 'SKIP_WEEKEND_MARKET_CLOSED', 'hist': 0.0 if hist_in is None else float(hist_in), 'histcolor': histcolor or 'amber', 'zone': 0, 'ha_side': ha_side(o, c), 'position': {0: 'FLAT', 1: 'LONG', -1: 'SHORT'}[self.pos], 'n_units': self._n_primary(), 'n_supp': self._n_supp(), 'n_total': self._n_total(), 'sl': None if not self.positions else next((p.sl for p in self.positions if p.is_primary), self.positions[0].sl), 'filled_action': None, 'weekend_skip': True, 'skip_weekends': True, 'entry_logic': 'prev_body_cross_xt', 'fill_mode': 'prev_body_immediate', 'mode': 'demo_trading_hist_rules_prev_body_025pt', 'weekend_skips': self.weekend_skips}
            if bar_key:
                self._seen_bars[bar_key] = copy.deepcopy(skip)
                while len(self._seen_bars) > MAX_SEEN_BARS:
                    self._seen_bars.popitem(last=False)
            return skip
        has_raw_execution = all((v is not None for v in raw_values))
        self.last_execution_source = 'raw_ohlc' if has_raw_execution else 'signal_ohlc_fallback'
        eo, eh, el, ec = (float(raw_open), float(raw_high), float(raw_low), float(raw_close)) if has_raw_execution else (o, h, l, c)
        trade_close = ec if has_raw_execution else c
        hist = 0.0 if hist_in is None else float(hist_in)
        zz, color = zone_from_sent(hist, histcolor)
        self.hist = hist
        self.warmed = True
        if skip_worst_hours is not None:
            self.skip_worst_hours = bool(skip_worst_hours)
        if focus_best_hours is not None:
            self.focus_best_hours = bool(focus_best_hours)
        hour = _parse_hour_utc4(time_str)
        lot = lot_for_hour(hour, self.focus_best_hours, DEFAULT_VOLUME)
        action = 'NONE'
        self.last_entry_reason = None
        closed_supps: list[dict] = []
        closed_primary: dict | None = None
        primary_closed = False
        sl_exits = 0
        filled_action = None
        just_opened: list[Position] = []
        sl_before = self._active_sl()
        if zz == 0:
            self.seen_amber = True
            self.pending = None
        if self.pending is not None:
            pa = self._do_open(
                self._entry_execution_price(self.pending.want, execution_prices, eo),
                self.pending.want,
                self.pending.is_primary,
                self.pending.lot,
                self.pending.signal_break_level,
                trade_close,
            )
            if pa in ('BUY', 'SELL', 'BUY_ADD', 'SELL_ADD'):
                action = pa
                filled_action = pa
                just_opened.append(self.positions[-1])
            self.pending = None
        if self._n_total() > 0:
            prior = [p for p in self.positions if all((p is not q for q in just_opened))]
            se, pc, cpri, csup = self._scan_stops(eh, el, eo, has_raw_execution, only=prior) if prior else (0, False, None, [])
            sl_exits += se
            if pc:
                primary_closed = True
                closed_primary = cpri
            closed_supps.extend(csup)
            if not self.positions:
                if sl_exits and filled_action is None:
                    action = 'EXIT'
                elif action == 'NONE':
                    action = 'HOLD'
            if self._n_total() > 0:
                if zz == 0:
                    action = self._flatten(ec)
                    self.run_side = 0
                    self.seen_amber = True
                else:
                    if effective_trail_every_candle():
                        for p in self.positions:
                            p.sl = self._trail_sl(p.sl, ec, self.pos)
                    # One entry per bar: skip SUPP if this bar already filled (e.g. deferred primary).
                    if (not just_opened
                            and zz == (1 if self.pos > 0 else -1)
                            and self._entry_ok(o, eh, el, ec, xtrend, self.pos, self.break_level, trade_close=trade_close)):
                        if not hour_allowed(hour, self.skip_worst_hours):
                            self.hour_skips += 1
                            self.last_entry_reason = f'SKIP_WORST_HOUR_{hour}'
                        else:
                            pa = self._open_on_cross(self.pos, False, lot, eh, el, eo, trade_close, self._entry_execution_price(self.pos, execution_prices, eo))
                            if pa in ('BUY_ADD', 'SELL_ADD'):
                                action = pa
                                filled_action = pa
                                just_opened.append(self.positions[-1])
                    if self._n_total() > 0 and self._mlevel(ec) < STOPOUT:
                        action = self._flatten(ec)
                        self.run_side = 0
        if (not just_opened
                and self._n_total() == 0 and self.run_side != 0
                and (zz == (1 if self.run_side > 0 else -1))
                and self._entry_ok(o, eh, el, ec, xtrend, self.run_side, self.break_level, trade_close=trade_close)):
            if not hour_allowed(hour, self.skip_worst_hours):
                self.hour_skips += 1
                self.last_entry_reason = f'SKIP_WORST_HOUR_{hour}'
            else:
                pa = self._open_on_cross(self.run_side, False, lot, eh, el, eo, trade_close, self._entry_execution_price(self.run_side, execution_prices, eo))
                if pa in ('BUY_ADD', 'SELL_ADD'):
                    action = pa
                    filled_action = pa
                    just_opened.append(self.positions[-1])
        if zz == 0:
            # Orange/amber: flatten everything and reset run + trailing levels.
            self.run_side = 0
            self.break_level = None
            self.last_trade_close = None
            self.pos = 0
            self.pending = None
        if (not just_opened
                and self._n_total() == 0 and self.run_side == 0 and self.seen_amber
                and (self.pending is None) and (self.prev_high is not None)):
            want = 0
            if zz == 1 and self._entry_ok(o, h, l, c, xtrend, 1, self.prev_high, is_primary=True, trade_close=trade_close):
                want = 1
            elif zz == -1 and self._entry_ok(o, h, l, c, xtrend, -1, self.prev_low or l, is_primary=True, trade_close=trade_close):
                want = -1
            if want:
                if not hour_allowed(hour, self.skip_worst_hours):
                    self.hour_skips += 1
                    self.last_entry_reason = f'SKIP_WORST_HOUR_{hour}'
                else:
                    pa = self._open_on_cross(want, True, lot, eh, el, eo, trade_close, self._entry_execution_price(want, execution_prices, eo))
                    if pa in ('BUY', 'SELL'):
                        action = pa
                        filled_action = pa
                        just_opened.append(self.positions[-1])
        if just_opened and self._n_total() > 0:
            live_new = [p for p in just_opened if any((p is q for q in self.positions))]
            if effective_entry_bar_mode() == 'test' and live_new:
                se, pc, cpri, csup = self._scan_stops(eh, el, eo, has_raw_execution, only=live_new)
                if se:
                    sl_exits += se
                    if pc:
                        primary_closed = True
                        closed_primary = cpri
                    closed_supps.extend(csup)
                    live_new = [p for p in live_new if any((p is q for q in self.positions))]
                    if not self.positions and action in ('BUY', 'SELL', 'BUY_ADD', 'SELL_ADD'):
                        action = 'EXIT'
            # Trail new tickets to THIS close in every entry-bar mode, including
            # 'defer'. Deferring must only skip the stop TEST on the entry bar —
            # skipping the trail too left the stop at entry±TSL, so the next bar
            # stopped the trade out for -(TSL+slippage)-spread even mid-trend.
            # _trail_sl only ever tightens toward profit, so this cannot widen risk.
            if effective_trail_every_candle() and effective_trail_entry_bar():
                for p in live_new:
                    p.sl = self._trail_sl(p.sl, ec, self.pos)
        if self._n_total() > 0 and self._mlevel(ec) < STOPOUT:
            action = self._flatten(ec)
            self.run_side = 0
        if action == 'NONE':
            action = 'HOLD' if self.pos else 'NONE'
        self.prev_high = h
        self.prev_low = l
        self.prev_open = o
        self.prev_close = c
        active_sl = self._active_sl()
        fill_px = just_opened[-1].entry if just_opened else eo
        if sl_exits:
            decision_reason = f'STOP_EXIT_{sl_exits}'
        elif action == 'EXIT' and zz == 0:
            decision_reason = 'ORANGE_HISTOGRAM_EXIT'
        elif filled_action:
            decision_reason = f'ENTRY_FILLED_{filled_action}'
        elif self.last_entry_reason:
            decision_reason = self.last_entry_reason
        elif not self.seen_amber and self.run_side == 0:
            decision_reason = 'WAITING_FOR_AMBER_ARM'
        elif zz == 0:
            decision_reason = 'AMBER_ARM_OR_FLAT'
        elif self._n_total() > 0:
            decision_reason = 'HOLDING_MANAGE_TRAIL'
        elif zz == 1 and not self.seen_amber:
            decision_reason = 'WAITING_FOR_AMBER_ARM'
        elif zz == -1 and not self.seen_amber:
            decision_reason = 'WAITING_FOR_AMBER_ARM'
        else:
            decision_reason = 'NO_ENTRY_SIGNAL'
        why_text = explain_decision(
            decision_reason,
            action=action,
            zone=color,
            hist=hist,
            seen_amber=self.seen_amber,
            n_total=self._n_total(),
            xtrend=xtrend,
            xt_gate=XT_GATE,
            trail=effective_trail_every_candle(),
            tsl=effective_tsl_pts(),
        )
        result = {'action': action, 'why': why_text, 'decision_reason': decision_reason, 'hist': hist, 'histcolor': color, 'zone': zz, 'ha_side': ha_side(o, c), 'position': {0: 'FLAT', 1: 'LONG', -1: 'SHORT'}[self.pos], 'n_units': self._n_primary(), 'n_supp': self._n_supp(), 'n_total': self._n_total(), 'sl': active_sl, 'pending_next_open': self.pending is not None, 'run_side': self.run_side, 'seen_amber': self.seen_amber, 'break_level': self.break_level, 'last_trade_close': self.last_trade_close, 'supp_close_rule': 'buy: next candle high > last trade candle high; sell: next candle low < last trade candle low', 'supp_positions': [{'entry': p.entry, 'sl': round(p.sl, 4), 'lot': p.lot, 'tsl_mode': 'pts_live'} for p in self.positions if not p.is_primary], 'closed_supps': closed_supps, 'closed_primary': closed_primary, 'primary_closed': primary_closed, 'sl_exits': sl_exits, 'filled_action': filled_action, 'fill_price': None if not just_opened else just_opened[-1].entry, 'sl_updated': active_sl, 'sl_changed': bool(active_sl is not None and (sl_before is None or abs(active_sl - sl_before) > 1e-09)), 'open_positions': [{'entry': p.entry, 'sl': round(p.sl, 4), 'lot': p.lot, 'is_primary': p.is_primary} for p in self.positions], 'skip_worst_hours': self.skip_worst_hours, 'focus_best_hours': self.focus_best_hours, 'best_lot_mult': BEST_LOT_MULT, 'fill_lot': getattr(self, '_last_fill_lot', DEFAULT_VOLUME), 'hour_utc4': hour, 'hour_skips': self.hour_skips, 'xt_skips': self.xt_skips, 'weekend_skips': self.weekend_skips, 'skip_weekends': SKIP_WEEKENDS, 'entry_logic': 'primary_body_xt__supp_trade_wick', 'fill_mode': 'live_quote' if execution_prices else ('prev_body_immediate' if not just_opened or just_opened[-1].is_primary else 'trade_wick_immediate'), 'close_confirm': False, 'tsl_pts': effective_tsl_pts(), 'tsl_desired': _env_float('TSL_PTS', 0.25), 'broker_min_stop_pts': _env_float('BROKER_MIN_STOP_PTS', 0.30), 'mode': 'latest_mods_primary_xt_supp_wick_025pt', 'xtrend': xtrend, 'xtrend_gate': XT_GATE, 'xtrend_gate_supp': effective_supp_xt_gate(), 'xtrend_touch_buf': XT_BUF, 'primary_wick_gate': PRIMARY_WICK_GATE, 'stop_slippage_pts': STOP_SLIPPAGE_PTS, 'stop_active_on_entry_bar': effective_entry_bar_mode() == 'test', 'entry_bar_mode': effective_entry_bar_mode(), 'trail_every_candle': effective_trail_every_candle(), 'trail_entry_bar': effective_trail_entry_bar(), 'execution_price_source': 'live_quote' if execution_prices else ('raw_ohlc' if has_raw_execution else 'signal_ohlc_fallback'), 'open': o, 'high': h, 'low': l, 'close': c, 'raw_open': eo, 'raw_high': eh, 'raw_low': el, 'raw_close': ec, 'spread_cost': SPREAD_COST, 'balance': round(self.balance, 4), 'duplicate_bar': False, 'dedupe_active': bool(bar_key), 'closed_units': ([{'kind': 'primary', **closed_primary}] if closed_primary else []) + [{'kind': 'supp', **x} for x in closed_supps], 'fill_sl': None if filled_action is None else self._init_sl(fill_px, 1 if 'BUY' in filled_action else -1), 'position_stops': [{'entry': p.entry, 'sl': round(p.sl, 4), 'lot': p.lot, 'is_primary': p.is_primary} for p in self.positions]}
        if bar_key:
            self._seen_bars[bar_key] = copy.deepcopy(result)
            self._seen_bars.move_to_end(bar_key)
            while len(self._seen_bars) > MAX_SEEN_BARS:
                self._seen_bars.popitem(last=False)
        return result



class Mt5LiveEngine:
    """Adapt the exact Demo engine to raw MT5 bars plus HA signal inputs."""

    def __init__(self):
        self.engine = LatestModsEngine()
        self.last_closed_time = None

    def snapshot(self) -> dict:
        return {
            "engine": self.engine.snapshot(),
            "last_closed_time": self.last_closed_time,
        }

    def restore(self, data: dict) -> None:
        self.engine.restore(data.get("engine") or {})
        self.last_closed_time = data.get("last_closed_time")

    def push(self, bar: dict, execution_prices: dict | None = None) -> dict:
        result = self.engine.push(
            float(bar["signal_open"]),
            float(bar["signal_high"]),
            float(bar["signal_low"]),
            float(bar["signal_close"]),
            float(bar["xtrend"]),
            float(bar["hist"]),
            bar.get("histcolor"),
            time_str=bar.get("time"),
            raw_open=float(bar.get("raw_open", bar["open"])),
            raw_high=float(bar.get("raw_high", bar["high"])),
            raw_low=float(bar.get("raw_low", bar["low"])),
            raw_close=float(bar.get("raw_close", bar["close"])),
            execution_prices=execution_prices,
        )
        self.last_closed_time = bar.get("time")
        return result

    def warmup(self, bars: list[dict], balance: float | None = None) -> None:
        """Replay history for indicator/run context without placing orders."""
        self.engine.reset()
        for bar in bars:
            self.push(bar)
        self.engine.pos = 0
        self.engine.positions = []
        self.engine.break_level = None
        self.engine.run_side = 0
        self.engine.pending = None
        self.engine.balance = float(balance if balance is not None else START_BALANCE)
        self.engine.hour_skips = 0
        self.engine.xt_skips = 0
        self.engine.weekend_skips = 0
        self.engine._seen_bars.clear()

