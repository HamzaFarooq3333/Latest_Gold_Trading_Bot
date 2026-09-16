"""Histogram / X-Trend rules engine for closed MT5 M15 candles.

This is the single source of truth for the trading rules. It is imported by
the Ali PC bridge (bridge_trader.py) for live execution and by the GCP desk
(server.py) for the /signal, /warmup and lab paper-trading endpoints. The two
copies in the repo (Ali PC Deployment/runtime and Google Console
Deployment/app) must stay byte-identical.

Inputs per bar
--------------
  signal OHLC   Heikin-Ashi candle (histogram, body-break and X-Trend gates)
  raw OHLC      broker candle (fills, stops, ATR)
  hist          EMA(WilderRSI(raw close, 3), 5) - 50
  histcolor     green / red / orange as coloured by the bridge (HIST_THRESH)
  xtrend        X-Trend line value (KJ GagaTrend on HA for Ali)

Two entry modes (ENTRY_EVERY_CANDLE)
------------------------------------
  1 (live)  The PRIMARY gate - this candle breaks the previous HA body AND
            sits fully clear of X-Trend - is evaluated on every green/red
            candle. Each pass opens its own ticket, stacking in the run
            direction up to MAXPOS. No amber arming, no supplementary logic.
  0         Classic rules: amber must appear first, one primary per colour
            run, then supplementary tickets on a raw wick break of the last
            trade candle (MAX_SUPP).

Exits (both modes)
------------------
  * per-ticket trailing stop that ratchets to each bar CLOSE, sized as
    TSL_ATR_MULT x Wilder ATR(14) of raw bars at entry (fixed for the trade),
    or TSL_TICKS x TSL_TICK_SIZE when the multiplier is 0
  * amber/orange histogram flattens everything and resets the run
  * margin stop-out below 50%

Every tunable that the desk exposes as a live control is read from the
environment at call time (effective_*), so a change saved on the dashboard
applies on the next bar without a process restart.
"""

from __future__ import annotations

import copy
import os
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone


# --------------------------------------------------------------------------- #
# Environment helpers - read live so dashboard controls apply without restart #
# --------------------------------------------------------------------------- #

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return int(default)


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def effective_volume() -> float:
    """Lot size for new tickets (VOLUME). The bridge sends this as the order volume."""
    lot = _env_float("VOLUME", 0.01)
    return lot if lot > 0 else 0.01


def effective_hist_thresh() -> float:
    """Histogram colour threshold: green >= +TH, red <= -TH, amber between."""
    return _env_float("HIST_THRESH", 10.0)


def effective_maxpos() -> int:
    return max(1, _env_int("MAXPOS", 20))


def effective_max_supp() -> int:
    return max(0, _env_int("MAX_SUPP", 10))


def effective_xt_gate() -> bool:
    """Primary entries must sit fully clear of the X-Trend line."""
    return _env_flag("XTREND_GATE", "1")


def effective_xt_gate_supp() -> bool:
    """Classic-mode SUPP entries also need X-Trend clearance. Default OFF."""
    return _env_flag("XTREND_GATE_SUPP", "0")


def effective_xt_buf() -> float:
    """Extra clearance demanded beyond a true touch of X-Trend (price units)."""
    return _env_float("XTREND_BUF", 0.0)


def effective_spread_cost() -> float:
    """Flat cost booked against the paper balance per fill (USD)."""
    return _env_float("SPREAD_COST", 0.06)


def effective_stop_slippage() -> float:
    """Adverse fill beyond a touched stop, price units. 0 = fill at the stop."""
    return _env_float("STOP_SLIPPAGE_PTS", 0.0)


def effective_disable_stop_loss() -> bool:
    """When ON the engine never stop-tests; only amber / margin exits remain."""
    return _env_flag("DISABLE_STOP_LOSS", "0")


def effective_best_lot_mult() -> float:
    return _env_float("BEST_LOT_MULT", 1.0)


def effective_tsl_pts() -> float:
    """Entry-fill geometry only (gap-open tolerance in _cross_fill). Not a stop."""
    return _env_float("TSL_PTS", 0.25)


def effective_tick_size() -> float:
    """Price value of one tick. XAUUSDm quotes 3 decimals, so 1 tick = 0.001."""
    size = _env_float("TSL_TICK_SIZE", 0.001)
    return size if size > 0 else 0.001


def effective_tsl_ticks() -> float:
    """Fallback trailing distance in ticks when TSL_ATR_MULT is 0 (1111 = $1.111)."""
    ticks = _env_float("TSL_TICKS", 1111.0)
    return ticks if ticks > 0 else 1111.0


def tsl_distance() -> float:
    """Fixed trailing distance in price units: TSL_TICKS x TSL_TICK_SIZE."""
    return effective_tsl_ticks() * effective_tick_size()


def effective_tsl_atr_mult() -> float:
    """Stop = mult x Wilder ATR(14) of raw M15 bars at entry, fixed per trade.

    A fixed tick distance only works while volatility stays where it was
    tuned; the walk-forward test rewarded the ratio, not the dollar figure.
    0 = use the fixed tick distance.
    """
    return _env_float("TSL_ATR_MULT", 0.0)


def effective_every_candle() -> bool:
    return _env_flag("ENTRY_EVERY_CANDLE", "0")


def effective_trail_every_candle() -> bool:
    return _env_flag("TRAIL_EVERY_CANDLE", "1")


def effective_trail_entry_bar() -> bool:
    """Trail the stop to the entry bar's close.

    The entry bar is never stop-TESTED (the wick that filled the trade must
    not kill it) but its close must still move the stop; otherwise the stop
    sits at entry +/- TSL into the next bar and any retrace closes the trade
    for exactly -(TSL + slippage) - spread.
    """
    return _env_flag("TRAIL_ENTRY_BAR", "1")


def effective_entry_bar_mode() -> str:
    """off / test / defer - whether the entry bar's own range can stop the trade."""
    mode = os.environ.get("ENTRY_BAR_MODE", "defer").strip().lower()
    return mode if mode in ("off", "test", "defer") else "defer"


def effective_skip_weekends() -> bool:
    return _env_flag("SKIP_WEEKENDS", "1")


def effective_skip_worst_hours() -> bool:
    return _env_flag("SKIP_WORST_HOURS", "0")


def effective_focus_best_hours() -> bool:
    return _env_flag("FOCUS_BEST_HOURS", "0")


# Account model (not desk controls - fixed per deployment).
CONTRACT = 100.0          # XAUUSD: 1.00 lot = 100 oz
STOPOUT = 0.5             # margin level below which everything is flattened
LEVERAGE = _env_float("LEVERAGE", 100.0)
MARGIN_MIN = _env_float("MARGIN_MIN_PCT", 75.0) / 100.0
START_BALANCE = _env_float("START_BALANCE", 100.0)
MAX_SEEN_BARS = 2048      # duplicate-bar cache size
ATR_PERIOD = 14

# Hour filters (UTC-4 clock). Both OFF on the Ali desk.
WORST_HOURS_UTC4 = {0, 3, 5, 17, 18}
BEST_HOURS_UTC4 = {2, 9, 10, 11, 19, 21}
HOUR_TZ_OFFSET = -4


# --------------------------------------------------------------------------- #
# Small pure helpers                                                           #
# --------------------------------------------------------------------------- #

def body_high(o: float, c: float) -> float:
    return o if o >= c else c


def body_low(o: float, c: float) -> float:
    return c if o >= c else o


def _parse_local_dt_utc4(time_str: str | None) -> datetime | None:
    if not time_str:
        return None
    try:
        ts = str(time_str).strip().replace("Z", "+00:00")
        if "T" not in ts and " " in ts:
            ts = ts.replace(" ", "T", 1)
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=HOUR_TZ_OFFSET)))
    except Exception:
        return None


def _parse_hour_utc4(time_str: str | None) -> int | None:
    local = _parse_local_dt_utc4(time_str)
    return None if local is None else int(local.hour)


def is_weekend_bar(time_str: str | None) -> bool:
    local = _parse_local_dt_utc4(time_str)
    return local is not None and local.weekday() >= 5


def hour_allowed(hour: int | None, skip_worst: bool) -> bool:
    return hour is None or not (skip_worst and hour in WORST_HOURS_UTC4)


def lot_for_hour(hour: int | None, focus_best: bool, base_lot: float) -> float:
    if focus_best and hour is not None and hour in BEST_HOURS_UTC4:
        return base_lot * effective_best_lot_mult()
    return base_lot


def zone(h: float) -> int:
    th = effective_hist_thresh()
    if h >= th:
        return 1
    if h <= -th:
        return -1
    return 0


def zone_from_sent(hist: float, color: str | None) -> tuple[int, str]:
    """Prefer the colour the bridge sent; fall back to the threshold."""
    c = str(color or "").strip().lower()
    if c in ("green", "g"):
        return 1, "green"
    if c in ("red", "r"):
        return -1, "red"
    if c in ("orange", "o", "amber", "a"):
        return 0, "orange"
    zz = zone(float(hist))
    return zz, "green" if zz > 0 else "red" if zz < 0 else "orange"


def ha_side(o: float, c: float) -> int:
    """+1 green HA (close > open), -1 red HA, 0 doji."""
    return 1 if c > o else -1 if c < o else 0


def explain_decision(code: str, *, action: str = "NONE", zone: str = "",
                     hist: float | None = None, seen_amber: bool = False,
                     n_total: int = 0, xtrend: float | None = None) -> str:
    """Human-readable reason for the desk's Why column, matching the active mode."""
    z = str(zone or "").lower()
    hist_s = f"{float(hist):.2f}" if hist is not None else "-"
    xt_s = f"{float(xtrend):.3f}" if xtrend is not None else "-"
    code_u = str(code or "NO_ENTRY_SIGNAL").upper()
    every = effective_every_candle()
    mult = effective_tsl_atr_mult()
    stop_s = f"{mult:g} x ATR(14)" if mult > 0 else f"{effective_tsl_ticks():g} ticks"
    if every:
        rules = (f"Rules: every green/red candle that breaks the previous body and "
                 f"{'clears X-Trend' if effective_xt_gate() else 'X-Trend gate OFF'} opens a ticket "
                 f"(stack to {effective_maxpos()}); stop {stop_s} trails each close; amber flattens.")
    else:
        rules = (f"Rules: amber arms -> primary on body break "
                 f"{'+ X-Trend clear' if effective_xt_gate() else '(X-Trend gate OFF)'} -> "
                 f"SUPP on last-trade wick break; stop {stop_s} trails each close; amber flattens.")
    mapping = {
        "SKIP_WEEKEND_MARKET_CLOSED": "No trade - weekend market closed (skip-weekends rule).",
        "WAITING_FOR_AMBER_ARM": (
            f"No trade - hist {hist_s} ({z or 'colour'}) but the engine is not amber-armed yet."),
        "AMBER_ARM_OR_FLAT": (
            f"No new entry - amber hist {hist_s} flattens any open run and resets the trail."),
        "SKIP_PRIMARY_PREVIOUS_BODY_GATE": (
            f"No entry - hist {hist_s} ({z}) but this candle did not break the previous body."),
        "SKIP_PRIMARY_XTREND_GATE": (
            f"No entry - candle touches/crosses X-Trend ({xt_s}); the whole candle must sit clear."),
        "SKIP_SUPPLEMENTARY_NO_TRADE_WICK": "No SUPP - no last-trade wick level stored yet.",
        "SKIP_SUPPLEMENTARY_TRADE_WICK_GATE": "No SUPP - this candle did not break the last trade candle's wick.",
        "SKIP_SUPPLEMENTARY_XTREND_GATE": f"No SUPP - candle touches X-Trend ({xt_s}) and XTREND_GATE_SUPP is ON.",
        "SKIP_OPPOSITE_SIDE_OPEN": (
            f"No entry - hist {hist_s} ({z}) is the opposite colour to the open run; wait for amber."),
        "REJECTED_LOCAL_RISK_OR_MAXPOS": "No trade - blocked by margin/risk or max open positions.",
        "REJECTED_MAX_SUPPLEMENTARY_POSITIONS": "No SUPP - already at max supplementary positions.",
        "ORANGE_HISTOGRAM_EXIT": "Exit - amber histogram flattens the open run and resets trailing stops.",
        "MARGIN_STOPOUT": "Exit - margin level fell below the stop-out floor; everything flattened.",
        "HOLDING_MANAGE_TRAIL": f"Holding {n_total} ticket(s); trailing stops. No new entry on this bar.",
        "NO_ENTRY_SIGNAL": f"No trade - hist {hist_s} ({z or '-'}); no entry gate passed on this bar.",
    }
    if code_u.startswith("STOP_EXIT_"):
        text = f"Stop exit - {code_u.replace('STOP_EXIT_', '')} ticket(s) hit the trailing stop on this bar."
    elif code_u.startswith("ENTRY_FILLED_"):
        side = code_u.replace("ENTRY_FILLED_", "")
        kind = "primary" if side in ("BUY", "SELL") else ("stacked" if every else "supplementary")
        text = f"Trade taken - filled {side} ({kind}). Hist {hist_s} ({z}); entry gate passed."
    elif code_u.startswith("SKIP_WORST_HOUR_"):
        text = f"No trade - worst-hour filter blocked entries at UTC-4 hour {code_u.replace('SKIP_WORST_HOUR_', '')}."
    else:
        text = mapping.get(code_u, f"{code_u.replace('_', ' ').title()} - action={action}.")
    return f"{text} {rules}"


# --------------------------------------------------------------------------- #
# Engine                                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class Position:
    entry: float
    sl: float
    is_primary: bool
    lot: float
    # Most favourable CLOSE seen since entry; the stop is always `tsl` behind it.
    best_price: float = 0.0
    # Stop distance for this ticket, fixed at entry. 0.0 = global tick distance
    # (what snapshots written before ATR stops restore to).
    tsl: float = 0.0
    # MT5 position ticket once the bridge has bound it, 0 until then. Lets the
    # bridge modify / close the exact ticket and reconcile broker-side closes.
    ticket: int = 0


class LatestModsEngine:
    """
    pos              +1 long run, -1 short run, 0 flat
    positions        open tickets, oldest first (the primary is index 0 while alive)
    run_side         classic mode: side of the primary taken this colour run
    seen_amber       classic mode: amber seen since the last primary (arms the next one)
    break_level      classic mode: raw wick of the last trade candle (SUPP trigger)
    prev_*           previous HA candle (body-break gate and primary fill level)
    """

    def __init__(self):
        self.reset()

    # ---- state -----------------------------------------------------------

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
        self.warmed = False
        self.balance = START_BALANCE
        self.hist = 0.0
        self.skip_worst_hours = effective_skip_worst_hours()
        self.focus_best_hours = effective_focus_best_hours()
        self.hour_skips = 0
        self.xt_skips = 0
        self.weekend_skips = 0
        self._last_fill_lot = effective_volume()
        self._atr, self._atr_n, self._prev_raw_close = 0.0, 0, None
        self.last_execution_source = "not_seen"
        self.last_entry_reason: str | None = None
        self._seen_bars: OrderedDict[str, dict] = OrderedDict()

    def snapshot(self) -> dict:
        """JSON-safe state used to survive service restarts."""
        return {
            "prev_high": self.prev_high, "prev_low": self.prev_low,
            "prev_open": self.prev_open, "prev_close": self.prev_close,
            "pos": self.pos,
            "positions": [asdict(p) for p in self.positions],
            "break_level": self.break_level,
            "last_trade_close": self.last_trade_close,
            "seen_amber": self.seen_amber, "run_side": self.run_side,
            "warmed": self.warmed, "balance": self.balance, "hist": self.hist,
            "hour_skips": self.hour_skips, "xt_skips": self.xt_skips,
            "weekend_skips": self.weekend_skips,
            "last_fill_lot": self._last_fill_lot,
            "atr": float(self._atr), "atr_n": int(self._atr_n),
            "prev_raw_close": self._prev_raw_close,
            "last_execution_source": self.last_execution_source,
            "seen_bars": list(self._seen_bars.items()),
        }

    def restore(self, data: dict) -> None:
        """Restore a snapshot; callers fall back to reset() on error."""
        self.reset()
        self.prev_high = data.get("prev_high")
        self.prev_low = data.get("prev_low")
        self.prev_open = data.get("prev_open")
        self.prev_close = data.get("prev_close")
        self.pos = int(data.get("pos") or 0)
        known = {f for f in Position.__dataclass_fields__}
        self.positions = [Position(**{k: v for k, v in p.items() if k in known})
                          for p in data.get("positions") or []]
        self.break_level = data.get("break_level")
        ltc = data.get("last_trade_close")
        self.last_trade_close = None if ltc is None else float(ltc)
        self.seen_amber = bool(data.get("seen_amber"))
        self.run_side = int(data.get("run_side") or 0)
        self.warmed = bool(data.get("warmed"))
        self.balance = float(data.get("balance", START_BALANCE))
        self.hist = float(data.get("hist", 0.0))
        self.hour_skips = int(data.get("hour_skips") or 0)
        self.xt_skips = int(data.get("xt_skips") or 0)
        self.weekend_skips = int(data.get("weekend_skips") or 0)
        self._last_fill_lot = float(data.get("last_fill_lot", effective_volume()))
        self._atr = float(data.get("atr") or 0.0)
        self._atr_n = int(data.get("atr_n") or 0)
        self._prev_raw_close = data.get("prev_raw_close")
        self.last_execution_source = str(data.get("last_execution_source") or "not_seen")
        self._seen_bars = OrderedDict(data.get("seen_bars") or [])
        while len(self._seen_bars) > MAX_SEEN_BARS:
            self._seen_bars.popitem(last=False)

    # ---- broker reconciliation (used by the bridge) ------------------------

    def bind_ticket(self, ticket: int) -> bool:
        """Attach the MT5 position ticket to the newest unbound ticket."""
        for p in reversed(self.positions):
            if not p.ticket:
                p.ticket = int(ticket)
                return True
        return False

    def reconcile(self, alive_tickets: set[int], mark_price: float) -> list[dict]:
        """Drop tickets the broker no longer holds (its SL fired, or a manual close).

        The engine would otherwise keep trailing a ghost, report the wrong
        stop, and its partial-close orders could hit a different live ticket.
        Only bound tickets are checked; an unbound one may still be filling.
        """
        gone = [p for p in self.positions if p.ticket and p.ticket not in alive_tickets]
        if not gone:
            return []
        records = []
        for p in gone:
            pnl = self._close_one(p, mark_price)
            records.append({"ticket": p.ticket, "entry": p.entry, "exit": mark_price,
                            "lot": p.lot, "pnl": round(pnl, 4),
                            "reason": "broker_closed", "is_primary": p.is_primary})
        self.positions = [p for p in self.positions if p not in gone]
        if not self.positions:
            self.pos = 0
        return records

    # ---- position maths ----------------------------------------------------

    def _n_total(self) -> int:
        return len(self.positions)

    def _n_primary(self) -> int:
        return sum(1 for p in self.positions if p.is_primary)

    def _n_supp(self) -> int:
        return sum(1 for p in self.positions if not p.is_primary)

    def _floating(self, px: float) -> float:
        return sum((px - p.entry if self.pos > 0 else p.entry - px) * CONTRACT * p.lot
                   for p in self.positions)

    def _used(self, px: float) -> float:
        if not self.positions or LEVERAGE <= 0:
            return 0.0
        return sum(p.lot for p in self.positions) * CONTRACT * px / LEVERAGE

    def _mlevel(self, px: float) -> float:
        u = self._used(px)
        return 999.0 if u <= 0 else (self.balance + self._floating(px)) / u

    def _can_add(self, px: float, lot: float) -> bool:
        if self._n_total() + 1 > effective_maxpos():
            return False
        fee = effective_spread_cost()
        need = lot * CONTRACT * px / LEVERAGE
        eq = self.balance + self._floating(px)
        proj_used = self._used(px) + need
        if proj_used > 0 and (eq - fee) / proj_used < MARGIN_MIN:
            return False
        return eq - self._used(px) >= need + fee

    def _update_atr(self, eh: float, el: float, ec: float) -> None:
        """Wilder ATR(14) on raw bars; snapshotted so ATR stops survive a restart."""
        prev_c = self._prev_raw_close
        tr = (eh - el) if prev_c is None else max(eh - el, abs(eh - prev_c), abs(el - prev_c))
        if self._atr_n < ATR_PERIOD:
            self._atr = (self._atr * self._atr_n + tr) / (self._atr_n + 1)
        else:
            self._atr += (tr - self._atr) / ATR_PERIOD
        self._atr_n += 1
        self._prev_raw_close = ec

    def _trade_tsl(self) -> float:
        """Stop distance for a ticket opened now: ATR-sized if enabled and warmed."""
        mult = effective_tsl_atr_mult()
        if mult > 0 and self._atr_n >= ATR_PERIOD and self._atr > 0:
            return mult * self._atr
        return tsl_distance()

    @staticmethod
    def _init_sl(entry: float, want: int, dist: float) -> float:
        return entry - dist if want > 0 else entry + dist

    def _advance_best(self, p: Position, close: float, side: int) -> None:
        """Ratchet this ticket's stop behind its best CLOSE.

        The close, not the bar extreme: a stop derived from a spike high can
        sit above the market at the close (invalid for the broker) and
        inflates backtests. max/min keeps the stop monotonic.
        """
        if not p.best_price:
            p.best_price = p.entry
        d = p.tsl or tsl_distance()
        if side > 0:
            p.best_price = max(p.best_price, float(close))
            p.sl = max(p.sl, p.best_price - d)
        else:
            p.best_price = min(p.best_price, float(close))
            p.sl = min(p.sl, p.best_price + d)

    def _active_sl(self) -> float | None:
        """The stop protecting the run: the primary's while it lives, else the tightest."""
        if not self.positions:
            return None
        primary = next((p for p in self.positions if p.is_primary), None)
        if primary is not None:
            return primary.sl
        stops = [p.sl for p in self.positions]
        return max(stops) if self.pos > 0 else min(stops)

    def _close_one(self, p: Position, px: float) -> float:
        pnl = (px - p.entry if self.pos > 0 else p.entry - px) * CONTRACT * p.lot
        self.balance += pnl
        return pnl

    def _flatten(self, px: float) -> str:
        self.balance += self._floating(px)
        self.positions = []
        self.pos = 0
        self.break_level = None
        self.last_trade_close = None
        return "EXIT"

    # ---- entry gates -------------------------------------------------------

    def _xt_clear(self, h: float, l: float, xt: float, want: int) -> bool:
        """Whole candle (wick included) must sit clear of X-Trend."""
        buf = effective_xt_buf()
        return l > xt + buf if want > 0 else h < xt - buf

    def _broke_prev_body(self, h: float, l: float, want: int) -> bool:
        if self.prev_open is None or self.prev_close is None:
            return False
        if want > 0:
            return h > body_high(self.prev_open, self.prev_close)
        return l < body_low(self.prev_open, self.prev_close)

    def _cross_fill(self, want: int, eo: float, eh: float, el: float) -> float:
        """Primary fill: the previous body level, or this raw open if it gapped through."""
        tol = effective_tsl_pts()
        if want > 0:
            lvl = body_high(self.prev_open, self.prev_close)
            if eo > lvl or (abs(el - eo) < 1e-9 and eo < lvl - tol):
                return eo
            return lvl
        lvl = body_low(self.prev_open, self.prev_close)
        if eo < lvl or (abs(eh - eo) < 1e-9 and eo > lvl + tol):
            return eo
        return lvl

    def _entry_ok(self, h: float, l: float, xt: float, want: int, level: float | None,
                  is_primary: bool) -> bool:
        if is_primary:
            if not self._broke_prev_body(h, l, want):
                self.last_entry_reason = "SKIP_PRIMARY_PREVIOUS_BODY_GATE"
                return False
            if effective_xt_gate() and not self._xt_clear(h, l, xt, want):
                self.xt_skips += 1
                self.last_entry_reason = "SKIP_PRIMARY_XTREND_GATE"
                return False
            return True
        if level is None:
            self.last_entry_reason = "SKIP_SUPPLEMENTARY_NO_TRADE_WICK"
            return False
        broke = h > float(level) if want > 0 else l < float(level)
        if not broke:
            self.last_entry_reason = "SKIP_SUPPLEMENTARY_TRADE_WICK_GATE"
            return False
        if effective_xt_gate_supp() and not self._xt_clear(h, l, xt, want):
            self.xt_skips += 1
            self.last_entry_reason = "SKIP_SUPPLEMENTARY_XTREND_GATE"
            return False
        return True

    # ---- opens -------------------------------------------------------------

    def _open_ticket(self, fill_px: float, want: int, is_primary: bool, lot: float,
                     signal_bl: float, trade_close: float | None) -> str:
        """Append one ticket. A primary also starts the run (pos / run_side)."""
        if not self._can_add(fill_px, lot):
            self.last_entry_reason = "REJECTED_LOCAL_RISK_OR_MAXPOS"
            return "NONE"
        if is_primary:
            self.pos = want
            self.run_side = want
            self.seen_amber = False
            self.positions = []
        else:
            if self.pos == 0:
                self.pos = self.run_side or want
            if not effective_every_candle() and self._n_supp() >= effective_max_supp():
                self.last_entry_reason = "REJECTED_MAX_SUPPLEMENTARY_POSITIONS"
                return "HOLD"
        side = self.pos
        d = self._trade_tsl()
        self.positions.append(Position(entry=fill_px, sl=self._init_sl(fill_px, side, d),
                                       is_primary=is_primary, lot=lot, best_price=fill_px, tsl=d))
        self.break_level = signal_bl
        self.last_trade_close = trade_close
        self._last_fill_lot = lot
        self.balance -= effective_spread_cost()
        if is_primary:
            return "BUY" if side > 0 else "SELL"
        return "BUY_ADD" if side > 0 else "SELL_ADD"

    def _open_primary(self, want: int, lot: float, h: float, l: float, eo: float, eh: float,
                      el: float, trade_close: float | None, execution_price: float | None) -> str:
        fill_px = float(execution_price) if execution_price is not None else self._cross_fill(want, eo, eh, el)
        return self._open_ticket(fill_px, want, True, lot, h if want > 0 else l, trade_close)

    def _open_supp(self, want: int, lot: float, eh: float, el: float, eo: float,
                   trade_close: float | None, execution_price: float | None) -> str:
        if self.break_level is None:
            self.last_entry_reason = "SKIP_SUPPLEMENTARY_NO_TRADE_WICK"
            return "NONE"
        if execution_price is not None:
            fill_px = float(execution_price)
        else:
            lvl = float(self.break_level)
            fill_px = eo if (eo > lvl if want > 0 else eo < lvl) else lvl
        return self._open_ticket(fill_px, want, False, lot, eh if want > 0 else el, trade_close)

    def _open_stack(self, want: int, lot: float, h: float, l: float, eo: float, eh: float,
                    el: float, trade_close: float | None, execution_price: float | None) -> str:
        """ENTRY_EVERY_CANDLE fill: primary gate, stacking semantics."""
        if self._n_total() == 0:
            return self._open_primary(want, lot, h, l, eo, eh, el, trade_close, execution_price)
        if self.pos != want:
            self.last_entry_reason = "SKIP_OPPOSITE_SIDE_OPEN"
            return "NONE"
        fill_px = float(execution_price) if execution_price is not None else self._cross_fill(want, eo, eh, el)
        return self._open_ticket(fill_px, want, False, lot, h if want > 0 else l, trade_close)

    # ---- stops -------------------------------------------------------------

    def _scan_stops(self, eh: float, el: float, only: list[Position] | None = None):
        """Close every ticket whose stop the bar's raw range touched."""
        sl_exits, closed_primary, closed_supps = 0, None, []
        if effective_disable_stop_loss() or not self.positions:
            return sl_exits, closed_primary, closed_supps
        check = None if only is None else {id(p) for p in only}
        slip = effective_stop_slippage()
        survivors: list[Position] = []
        for p in self.positions:
            if check is not None and id(p) not in check:
                survivors.append(p)
                continue
            hit = el <= p.sl if self.pos > 0 else eh >= p.sl
            if not hit:
                survivors.append(p)
                continue
            # Fill at the slipped stop, never at a worse gap-through open.
            stop_fill = p.sl - slip if self.pos > 0 else p.sl + slip
            pnl = self._close_one(p, stop_fill)
            sl_exits += 1
            rec = {"entry": p.entry, "exit": stop_fill, "lot": p.lot, "ticket": p.ticket,
                   "pnl": round(pnl, 4), "reason": "primary_tsl" if p.is_primary else "supp_tsl"}
            if p.is_primary:
                closed_primary = rec
            else:
                closed_supps.append(rec)
        self.positions = survivors
        if not self.positions:
            self.pos = 0
        return sl_exits, closed_primary, closed_supps

    @staticmethod
    def _entry_execution_price(want: int, execution_prices: dict | None) -> float | None:
        """Live ask/bid at the moment the closed bar is processed, or None.

        None makes the open helpers fill at the previous-body break level (or
        the raw open on a gap) - the backtest convention the desk tester and
        the walk-forward reports use. The old fallback returned the raw open,
        so lab/backtest fills silently differed from every other backtest.
        """
        if not execution_prices:
            return None
        value = execution_prices.get("BUY" if want > 0 else "SELL")
        return None if value is None else float(value)

    # ---- one closed bar ----------------------------------------------------

    def push(self, o: float, h: float, l: float, c: float, xtrend: float, hist_in: float | None,
             histcolor: str | None = None, skip_worst_hours: bool | None = None,
             focus_best_hours: bool | None = None, time_str: str | None = None,
             raw_open: float | None = None, raw_high: float | None = None,
             raw_low: float | None = None, raw_close: float | None = None,
             execution_prices: dict | None = None) -> dict:
        raw_values = (raw_open, raw_high, raw_low, raw_close)
        if any(v is not None for v in raw_values) and not all(v is not None for v in raw_values):
            raise ValueError("raw_open, raw_high, raw_low and raw_close must be sent together")

        # A retried bar replays its cached result; state never advances twice.
        bar_key = str(time_str or "").strip()
        if bar_key and bar_key in self._seen_bars:
            duplicate = copy.deepcopy(self._seen_bars[bar_key])
            duplicate["duplicate_bar"] = True
            duplicate["dedupe_key"] = bar_key
            return duplicate

        hist = 0.0 if hist_in is None else float(hist_in)
        if effective_skip_weekends() and is_weekend_bar(time_str):
            self.weekend_skips += 1
            self.warmed = True
            return self._remember(bar_key, {
                "action": "NONE", "decision_reason": "SKIP_WEEKEND_MARKET_CLOSED",
                "why": explain_decision("SKIP_WEEKEND_MARKET_CLOSED", hist=hist, zone="amber",
                                        seen_amber=self.seen_amber, n_total=self._n_total()),
                "hist": hist, "histcolor": histcolor or "amber", "zone": 0, "ha_side": ha_side(o, c),
                "position": {0: "FLAT", 1: "LONG", -1: "SHORT"}[self.pos],
                "n_units": self._n_primary(), "n_supp": self._n_supp(), "n_total": self._n_total(),
                "sl": self._active_sl(), "filled_action": None, "weekend_skip": True,
                "skip_weekends": True, "weekend_skips": self.weekend_skips,
                "mode": self._mode_label(), "duplicate_bar": False,
            })

        has_raw = all(v is not None for v in raw_values)
        self.last_execution_source = "raw_ohlc" if has_raw else "signal_ohlc_fallback"
        eo, eh, el, ec = (float(raw_open), float(raw_high), float(raw_low), float(raw_close)) if has_raw else (o, h, l, c)
        trade_close = ec
        self._update_atr(eh, el, ec)
        zz, color = zone_from_sent(hist, histcolor)
        self.hist = hist
        self.warmed = True
        if skip_worst_hours is not None:
            self.skip_worst_hours = bool(skip_worst_hours)
        if focus_best_hours is not None:
            self.focus_best_hours = bool(focus_best_hours)
        hour = _parse_hour_utc4(time_str)
        lot = lot_for_hour(hour, self.focus_best_hours, effective_volume())
        every = effective_every_candle()

        action = "NONE"
        self.last_entry_reason = None
        closed_supps: list[dict] = []
        closed_primary: dict | None = None
        sl_exits = 0
        filled_action = None
        just_opened: list[Position] = []
        stopout = False
        sl_before = self._active_sl()

        if zz == 0:
            self.seen_amber = True

        # 1. Stops on tickets that were open before this bar, then amber flatten
        #    or trail everything that survived.
        if self.positions:
            se, cpri, csup = self._scan_stops(eh, el)
            sl_exits += se
            closed_primary = cpri or closed_primary
            closed_supps.extend(csup)
            if not self.positions:
                action = "EXIT" if sl_exits else "HOLD"
            elif zz == 0:
                action = self._flatten(ec)
                self.run_side = 0
            else:
                if effective_trail_every_candle():
                    for p in self.positions:
                        self._advance_best(p, ec, self.pos)
                # Classic mode: at most one new ticket per bar - a SUPP on the
                # raw wick break of the last trade candle.
                if (not every and zz == self.pos
                        and self._entry_ok(eh, el, xtrend, self.pos, self.break_level, False)):
                    if not hour_allowed(hour, self.skip_worst_hours):
                        self.hour_skips += 1
                        self.last_entry_reason = f"SKIP_WORST_HOUR_{hour}"
                    else:
                        pa = self._open_supp(self.pos, lot, eh, el, eo, trade_close,
                                             self._entry_execution_price(self.pos, execution_prices))
                        if pa in ("BUY_ADD", "SELL_ADD"):
                            action = filled_action = pa
                            just_opened.append(self.positions[-1])

        # 2. Classic mode: flat but the run is still alive - re-enter as SUPP.
        if (not every and not just_opened and not self.positions and self.run_side != 0
                and zz == self.run_side
                and self._entry_ok(eh, el, xtrend, self.run_side, self.break_level, False)):
            if not hour_allowed(hour, self.skip_worst_hours):
                self.hour_skips += 1
                self.last_entry_reason = f"SKIP_WORST_HOUR_{hour}"
            else:
                pa = self._open_supp(self.run_side, lot, eh, el, eo, trade_close,
                                     self._entry_execution_price(self.run_side, execution_prices))
                if pa in ("BUY_ADD", "SELL_ADD"):
                    action = filled_action = pa
                    just_opened.append(self.positions[-1])

        # 3. Amber ends the run whatever mode we are in.
        if zz == 0:
            self.run_side = 0
            self.break_level = None
            self.last_trade_close = None
            self.pos = 0

        # 4. Primary gate. Every-candle mode stacks; classic needs amber arming.
        if zz != 0 and not just_opened and self.prev_high is not None:
            want = zz
            can_try = (self._n_total() == 0 or self.pos == zz) if every else (
                self._n_total() == 0 and self.run_side == 0 and self.seen_amber)
            if can_try and self._entry_ok(h, l, xtrend, want, None, True):
                if not hour_allowed(hour, self.skip_worst_hours):
                    self.hour_skips += 1
                    self.last_entry_reason = f"SKIP_WORST_HOUR_{hour}"
                else:
                    px = self._entry_execution_price(want, execution_prices)
                    pa = (self._open_stack if every else self._open_primary)(
                        want, lot, h, l, eo, eh, el, trade_close, px)
                    if pa in ("BUY", "SELL", "BUY_ADD", "SELL_ADD"):
                        action = filled_action = pa
                        just_opened.append(self.positions[-1])
            elif every and self._n_total() and self.pos != zz:
                self.last_entry_reason = "SKIP_OPPOSITE_SIDE_OPEN"

        # 5. Entry-bar handling: optional stop test, then trail to this close.
        if just_opened and self.positions:
            live_new = [p for p in just_opened if p in self.positions]
            if effective_entry_bar_mode() == "test" and live_new:
                se, cpri, csup = self._scan_stops(eh, el, only=live_new)
                if se:
                    sl_exits += se
                    closed_primary = cpri or closed_primary
                    closed_supps.extend(csup)
                    live_new = [p for p in live_new if p in self.positions]
                    if not self.positions and action in ("BUY", "SELL", "BUY_ADD", "SELL_ADD"):
                        action = "EXIT"
            if effective_trail_every_candle() and effective_trail_entry_bar():
                for p in live_new:
                    self._advance_best(p, ec, self.pos)

        # 6. Margin stop-out.
        if self.positions and self._mlevel(ec) < STOPOUT:
            action = self._flatten(ec)
            self.run_side = 0
            stopout = True

        if action == "NONE":
            action = "HOLD" if self.pos else "NONE"

        self.prev_high, self.prev_low, self.prev_open, self.prev_close = h, l, o, c

        active_sl = self._active_sl()
        if sl_exits:
            decision_reason = f"STOP_EXIT_{sl_exits}"
        elif stopout:
            decision_reason = "MARGIN_STOPOUT"
        elif action == "EXIT" and zz == 0:
            decision_reason = "ORANGE_HISTOGRAM_EXIT"
        elif filled_action:
            decision_reason = f"ENTRY_FILLED_{filled_action}"
        elif self.last_entry_reason:
            decision_reason = self.last_entry_reason
        elif zz == 0:
            decision_reason = "AMBER_ARM_OR_FLAT"
        elif self.positions:
            decision_reason = "HOLDING_MANAGE_TRAIL"
        elif not every and not self.seen_amber and self.run_side == 0:
            decision_reason = "WAITING_FOR_AMBER_ARM"
        else:
            decision_reason = "NO_ENTRY_SIGNAL"
        why_text = explain_decision(decision_reason, action=action, zone=color, hist=hist,
                                    seen_amber=self.seen_amber, n_total=self._n_total(), xtrend=xtrend)
        closed_units = ([{"kind": "primary", **closed_primary}] if closed_primary else []) + \
                       [{"kind": "supp", **x} for x in closed_supps]
        result = {
            "action": action, "why": why_text, "decision_reason": decision_reason,
            "hist": hist, "histcolor": color, "zone": zz, "ha_side": ha_side(o, c),
            "position": {0: "FLAT", 1: "LONG", -1: "SHORT"}[self.pos],
            "n_units": self._n_primary(), "n_supp": self._n_supp(), "n_total": self._n_total(),
            "sl": active_sl, "sl_updated": active_sl,
            "sl_changed": bool(active_sl is not None and (sl_before is None or abs(active_sl - sl_before) > 1e-9)),
            "run_side": self.run_side, "seen_amber": self.seen_amber,
            "break_level": self.break_level, "last_trade_close": self.last_trade_close,
            "filled_action": filled_action,
            "fill_price": just_opened[-1].entry if just_opened else None,
            # The new ticket's own stop (ATR-sized, already trailed to this close):
            # this is what the bridge sends with the market order.
            "fill_sl": just_opened[-1].sl if just_opened and just_opened[-1] in self.positions else None,
            "fill_lot": self._last_fill_lot,
            "open_positions": [{"entry": p.entry, "sl": round(p.sl, 4), "lot": p.lot,
                                "is_primary": p.is_primary, "ticket": p.ticket,
                                "tsl": round(p.tsl or tsl_distance(), 4)} for p in self.positions],
            "closed_primary": closed_primary, "closed_supps": closed_supps,
            "closed_units": closed_units, "sl_exits": sl_exits,
            "skip_worst_hours": self.skip_worst_hours, "focus_best_hours": self.focus_best_hours,
            "hour_utc4": hour, "hour_skips": self.hour_skips, "xt_skips": self.xt_skips,
            "weekend_skips": self.weekend_skips, "skip_weekends": effective_skip_weekends(),
            "mode": self._mode_label(),
            "entry_every_candle": every, "hist_thresh": effective_hist_thresh(),
            "tsl_mode": "atr" if effective_tsl_atr_mult() > 0 else "ticks",
            "tsl_atr_mult": effective_tsl_atr_mult(), "atr": round(float(self._atr), 4),
            "tsl_ticks": effective_tsl_ticks(), "tsl_tick_size": effective_tick_size(),
            "tsl_distance": round(self._trade_tsl(), 4),
            "xtrend": xtrend, "xtrend_gate": effective_xt_gate(),
            "xtrend_gate_supp": effective_xt_gate_supp(), "xtrend_touch_buf": effective_xt_buf(),
            "stop_slippage_pts": effective_stop_slippage(),
            "entry_bar_mode": effective_entry_bar_mode(),
            "trail_every_candle": effective_trail_every_candle(),
            "trail_entry_bar": effective_trail_entry_bar(),
            "execution_price_source": "live_quote" if execution_prices else self.last_execution_source,
            "open": o, "high": h, "low": l, "close": c,
            "raw_open": eo, "raw_high": eh, "raw_low": el, "raw_close": ec,
            "spread_cost": effective_spread_cost(), "balance": round(self.balance, 4),
            "duplicate_bar": False, "dedupe_active": bool(bar_key),
        }
        return self._remember(bar_key, result)

    def _mode_label(self) -> str:
        entry = "every_candle" if effective_every_candle() else "classic_amber_supp"
        stop = f"atr{effective_tsl_atr_mult():g}" if effective_tsl_atr_mult() > 0 else f"ticks{effective_tsl_ticks():g}"
        return f"{entry}__{stop}"

    def _remember(self, bar_key: str, result: dict) -> dict:
        if bar_key:
            self._seen_bars[bar_key] = copy.deepcopy(result)
            self._seen_bars.move_to_end(bar_key)
            while len(self._seen_bars) > MAX_SEEN_BARS:
                self._seen_bars.popitem(last=False)
        return result


class Mt5LiveEngine:
    """Adapts the bridge's MT5 bar dicts (signal_* / raw_*) to LatestModsEngine."""

    def __init__(self):
        self.engine = LatestModsEngine()
        self.last_closed_time = None

    def snapshot(self) -> dict:
        return {"engine": self.engine.snapshot(), "last_closed_time": self.last_closed_time}

    def restore(self, data: dict) -> None:
        self.engine.restore(data.get("engine") or {})
        self.last_closed_time = data.get("last_closed_time")

    def push(self, bar: dict, execution_prices: dict | None = None) -> dict:
        result = self.engine.push(
            float(bar["signal_open"]), float(bar["signal_high"]),
            float(bar["signal_low"]), float(bar["signal_close"]),
            float(bar["xtrend"]), float(bar["hist"]), bar.get("histcolor"),
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
        """Replay history for indicator/run context without keeping any trades."""
        self.engine.reset()
        for bar in bars:
            self.push(bar)
        e = self.engine
        e.pos, e.positions, e.break_level, e.last_trade_close, e.run_side = 0, [], None, None, 0
        e.balance = float(balance if balance is not None else START_BALANCE)
        e.hour_skips = e.xt_skips = e.weekend_skips = 0
        e._seen_bars.clear()
