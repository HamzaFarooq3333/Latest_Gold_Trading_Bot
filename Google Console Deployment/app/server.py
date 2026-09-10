"""
Asim FB Agent — Histogram rules engine + Asim execution extras.

Trading brain matches lambda_histogram_rules (LatestModsEngine):
previous-body cross, fill at that body (or this open on a far-side gap),
trail every candle close, stop live from the next bar, 0.25-pt TSL + 0.25-pt
slippage, optional raw_* execution.

TradingView (or the lab) sends one closed 15-minute Heikin Ashi bar to
POST /signal. LatestModsEngine.push() looks at that bar plus the previous
bar and either:

  BUY / SELL           first trade of a colour run (primary)
  BUY_ADD / SELL_ADD   extra trade in the same run (supplementary)
  EXIT                 flatten everything (amber, or broker-style stop-out)
  HOLD / NONE          nothing new this bar

Rules (as specified 2026-08-22)
-------------------------------
1. Histogram must have been amber at least once (seen_amber).
2. After amber, check each coloured hist bar until one qualifies.
   HA candle colour does NOT gate entries or exits — only the histogram does.
3. Watch the next candle after a completed bar. Buy when that candle
   crosses the previous candle's BODY (max of previous open/close), not
   the wick. Example: open 4000, close 4003, wick 4005 — next candle
   crossing 4003 is the buy; fill is 4003 itself (or this open if it
   already gapped through). Far-side visual gap fills at this open.
   Sell is the mirror on the previous body low.
4. Chart arrows sit on the candle that made the cross.
5. One primary per colour run. After that, only supplementary until amber.
6. Supplementary uses the same previous-body cross.
7. Each position has its own 0.25-point trailing stop. After every closed
   candle the stop moves with that close. The entry candle is NOT stopped
   against its own wick. The trailed stop becomes live on the next candle.
   Trailing is not frozen on opposite HA.
8. Amber (histogram) is the only colour event that stops a run and flattens.
   A red HA candle during a green-hist buy does NOT exit or kill the run —
   the next bar may be green again and make a new high. Sell is the mirror.
   Opposite hist without amber also does not reset the run.

Top-level OHLC is the Heikin Ashi signal series. Optional raw_* fields are the
executable market series. Without raw_*, execution falls back to signal OHLC
and reports that limitation in the response.

Hardening added on top of the copied engine (2026-08-23)
--------------------------------------------------------
* Duplicate-bar idempotency cache keyed on bar timestamp. A retried webhook
  replays its cached result instead of advancing state twice.
* Engine state (positions, balance, run state, dedupe cache) is persisted
  atomically and restored on boot, so a systemd restart no longer forgets an
  open trade.
* A single RLock serialises /signal, /warmup and /reset.
* SPREAD_COST is charged per fill and included in the margin gate, matching the
  dashboard backtest and the Lambda paper profiles.
* _can_add also checks plain affordability (margin + fee out of free equity),
  which the dashboard always did and the engine did not.
* Partial stop-outs emit POSITIONS_CLOSE_PARTIAL_SYMBOL orders, and SL_MODIFY
  carries per-ticket stops; closed_* rows carry their lot.
* A partial raw_* group is a 400, not a 500.

Strategy rules are Histogram rules (prev-body fill). None of the extras
above change the cross, fill, trail, or next-bar stop.
"""
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

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

API_KEY = os.environ.get("API_KEY", "")
DEFAULT_VOLUME = float(os.environ.get("VOLUME", "0.01"))
SYMBOL = os.environ.get("SYMBOL", "XAUUSD")
MODEL_LABEL = os.environ.get("MODEL_NAME", "Asim FB Agent")
TH = float(os.environ.get("HIST_THRESH", "10"))
LEVERAGE = float(os.environ.get("LEVERAGE", "100"))
MARGIN_MIN = float(os.environ.get("MARGIN_MIN_PCT", "75")) / 100.0
MAXPOS = int(os.environ.get("MAXPOS", "20"))
MAX_SUPP = int(os.environ.get("MAX_SUPP", "10"))
# Playbook A: 0.25-pt trail every closed candle.
TSL_PTS = float(os.environ.get("TSL_PTS", "0.25"))


def effective_tsl_pts() -> float:
    try:
        return float(os.environ.get("TSL_PTS", str(TSL_PTS)))
    except (TypeError, ValueError):
        return TSL_PTS
# Stop orders are market orders once touched. This conservative estimate is
# applied beyond the stop level; set to 0 only to reproduce the old perfect-fill
# assumption.
STOP_SLIPPAGE_PTS = float(os.environ.get("STOP_SLIPPAGE_PTS", "0.25"))
CONTRACT = 100.0
STOPOUT = 0.50
BEST_LOT_MULT = float(os.environ.get("BEST_LOT_MULT", "1.0"))
# Flat USD entry fee per fill, matching the dashboard backtest and the Lambda
# paper profiles (spread_cost 0.30). The engine used to trade fee-free, so its
# margin gate was looser than the dashboard's; charging it here keeps EC2,
# dashboard and paper accounts on one set of economics. Set 0 for fee-free.
# Raw ECN charges commission, not spread: ~USD 3 per lot per side, so a 0.01
# lot round-turn is ~USD 0.06, not 0.30. The old 0.30 was a spread-account
# figure and over-charges a Raw ECN fill by 5x, which tightens the margin gate
# and rejects trades that should pass. Re-measure against real Vantage fills.
SPREAD_COST = float(os.environ.get("SPREAD_COST", "0.06"))
# Engine state survives a systemd restart / EC2 reboot. Without this a restart
# mid-trade silently forgets open positions, balance and run state.
STATE_FILE = Path(os.environ.get("STATE_FILE", "/opt/asim-gcp/data/engine_state.json"))
MAX_SEEN_BARS = 2048

WORST_HOURS_UTC4 = {0, 3, 5, 17, 18}
BEST_HOURS_UTC4 = {2, 9, 10, 11, 19, 21}
HOUR_TZ_OFFSET = -4

# Matches the live Vantage demo (account 25989834, USD 100). The margin gate,
# the 75% floor and the 50% stop-out are all sized off this, so a default that
# does not match the broker balance silently changes which trades are allowed.
START_BALANCE = float(os.environ.get("START_BALANCE", "100"))
SKIP_WORST_HOURS = os.environ.get("SKIP_WORST_HOURS", "0").strip().lower() in ("1", "true", "yes", "on")
FOCUS_BEST_HOURS = os.environ.get("FOCUS_BEST_HOURS", "0").strip().lower() in ("1", "true", "yes", "on")
XT_GATE = os.environ.get("XTREND_GATE", "1").strip().lower() in ("1", "true", "yes", "on")

# Separate SUPP gate, OFF by default: SUPP only needs the last-trade wick break.
XT_GATE_SUPP = os.environ.get("XTREND_GATE_SUPP", "0").strip().lower() in ("1", "true", "yes", "on")
# Extra clearance demanded beyond a true touch. 0 blocks every candle the line
# passes through and nothing else; raising it also rejects candles that are
# clear but close, which costs real trades.
XT_BUF = float(os.environ.get("XTREND_BUF", "0"))

# Kept for health/dashboard compatibility. Entry requires this candle to take
# out the previous candle's BODY, not the wick. PRIMARY_WICK_GATE is ignored.
PRIMARY_WICK_GATE = os.environ.get("PRIMARY_WICK_GATE", "0").strip().lower() in ("1", "true", "yes", "on")

# How the ENTRY candle is treated for risk vs reward.
#   "off"    (legacy) entry bar is exempt from the stop test but still trails to
#            its own close -> risk ignored, reward banked. Asymmetric.
#   "test"   entry bar is stop-tested against its full low/high AND trails.
#            Pessimistic bound: the bar's extreme may pre-date the mid-bar fill.
#   "defer"  entry bar is neither stop-tested nor trailed; the stop stays at
#            entry -/+ TSL and normal trailing starts on the next candle.
#            Neutral: the unobservable part of the entry bar is simply not used.
# Trail the stop to the entry bar's close. The entry bar is never stop-TESTED,
# but its close must still move the stop, otherwise the stop stays at
# entry +/- TSL into the next bar and any retrace closes the trade for exactly
# -(TSL + slippage) - spread.
TRAIL_ENTRY_BAR = os.environ.get("TRAIL_ENTRY_BAR", "1").strip().lower() in ("1", "true", "yes", "on")

ENTRY_BAR_MODE = os.environ.get("ENTRY_BAR_MODE", "defer").strip().lower()
if ENTRY_BAR_MODE not in ("off", "test", "defer"):
    ENTRY_BAR_MODE = "defer"


# FIX (Histogram rules): body top/bottom of a candle = max/min of open & close.
# OLD KJ used the wick (high/low). Example: O=4000 C=4003 H=4005 → body_high=4003,
# not 4005. See errors/updated_histogram_rules_wrong_trade_fixes.md §3.
def body_high(o: float, c: float) -> float:
    return o if o >= c else c


def body_low(o: float, c: float) -> float:
    return c if o >= c else o


def _parse_hour_utc4(time_str: Optional[str]) -> Optional[int]:
    if not time_str:
        return None
    try:
        ts = str(time_str).strip().replace("Z", "+00:00")
        if "T" not in ts and " " in ts:
            ts = ts.replace(" ", "T", 1)
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(timezone(timedelta(hours=HOUR_TZ_OFFSET)))
        return int(local.hour)
    except Exception:
        return None


def hour_allowed(hour: Optional[int], skip_worst: bool) -> bool:
    if hour is None:
        return True
    if skip_worst and hour in WORST_HOURS_UTC4:
        return False
    return True


def lot_for_hour(hour: Optional[int], focus_best: bool, base_lot: float) -> float:
    if focus_best and hour is not None and hour in BEST_HOURS_UTC4:
        return base_lot * BEST_LOT_MULT
    return base_lot


class Candle(BaseModel):
    # Signal OHLC. Existing feeds send Heikin Ashi here.
    open: float
    high: float
    low: float
    close: float
    # Optional executable market OHLC for the same timestamp. All four raw
    # fields are required together.
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
    """Reject a partial raw_* group as a 400, not an unhandled 500."""
    for c in candles:
        vals = (c.raw_open, c.raw_high, c.raw_low, c.raw_close)
        if any(v is not None for v in vals) and not all(v is not None for v in vals):
            raise HTTPException(
                status_code=400,
                detail="raw_open, raw_high, raw_low and raw_close must be sent together",
            )


def zone(h: float) -> int:
    if h >= TH:
        return 1
    if h <= -TH:
        return -1
    return 0


def zone_from_sent(hist: float, color: Optional[str]) -> tuple[int, str]:
    c = str(color or "").strip().lower()
    if c in ("green", "g"):
        return 1, "green"
    if c in ("red", "r"):
        return -1, "red"
    if c in ("orange", "o", "amber", "a"):
        return 0, "orange"
    zz = zone(float(hist))
    name = "green" if zz > 0 else "red" if zz < 0 else "orange"
    return zz, name


def ha_side(o: float, c: float) -> int:
    """+1 green HA (close > open), -1 red HA, 0 doji."""
    if c > o:
        return 1
    if c < o:
        return -1
    return 0


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
        self._last_fill_lot = DEFAULT_VOLUME
        self.last_execution_source = "not_seen"
        # Bounded idempotency cache. TradingView and HTTP clients retry bars;
        # processing the same timestamp twice must never advance state twice.
        self._seen_bars: "OrderedDict[str, dict]" = OrderedDict()

    def snapshot(self) -> dict:
        """JSON-safe state used to survive service restarts."""
        return {
            "prev_high": self.prev_high,
            "prev_low": self.prev_low,
            "prev_open": self.prev_open,
            "prev_close": self.prev_close,
            "pos": self.pos,
            "positions": [asdict(p) for p in self.positions],
            "break_level": self.break_level,
            "last_trade_close": self.last_trade_close,
            "seen_amber": self.seen_amber,
            "run_side": self.run_side,
            "pending": None if self.pending is None else asdict(self.pending),
            "warmed": self.warmed,
            "balance": self.balance,
            "hist": self.hist,
            "skip_worst_hours": self.skip_worst_hours,
            "focus_best_hours": self.focus_best_hours,
            "hour_skips": self.hour_skips,
            "xt_skips": self.xt_skips,
            "last_fill_lot": self._last_fill_lot,
            "last_execution_source": self.last_execution_source,
            "seen_bars": list(self._seen_bars.items()),
        }

    def restore(self, data: dict) -> None:
        """Restore a validated snapshot; callers fall back to reset on error."""
        self.prev_high = data.get("prev_high")
        self.prev_low = data.get("prev_low")
        self.prev_open = data.get("prev_open")
        self.prev_close = data.get("prev_close")
        self.pos = int(data.get("pos") or 0)
        self.positions = [Position(**p) for p in data.get("positions") or []]
        self.break_level = data.get("break_level")
        saved_trade_close = data.get("last_trade_close")
        self.last_trade_close = None if saved_trade_close is None else float(saved_trade_close)
        self.seen_amber = bool(data.get("seen_amber"))
        self.run_side = int(data.get("run_side") or 0)
        pending = data.get("pending")
        self.pending = Pending(**pending) if pending else None
        self.warmed = bool(data.get("warmed"))
        self.balance = float(data.get("balance", START_BALANCE))
        self.hist = float(data.get("hist", 0.0))
        # Hour filters are deploy policy, not trade state.
        self.skip_worst_hours = SKIP_WORST_HOURS
        self.focus_best_hours = FOCUS_BEST_HOURS
        self.hour_skips = int(data.get("hour_skips") or 0)
        self.xt_skips = int(data.get("xt_skips") or 0)
        self._last_fill_lot = float(data.get("last_fill_lot", DEFAULT_VOLUME))
        self.last_execution_source = str(data.get("last_execution_source") or "not_seen")
        self._seen_bars = OrderedDict(data.get("seen_bars") or [])
        while len(self._seen_bars) > MAX_SEEN_BARS:
            self._seen_bars.popitem(last=False)

    def _n_total(self) -> int:
        return len(self.positions)

    def _n_primary(self) -> int:
        return sum(1 for p in self.positions if p.is_primary)

    def _n_supp(self) -> int:
        return sum(1 for p in self.positions if not p.is_primary)

    def _floating(self, px: float) -> float:
        tot = 0.0
        for p in self.positions:
            tot += (px - p.entry if self.pos > 0 else p.entry - px) * CONTRACT * p.lot
        return tot

    def _used(self, px: float) -> float:
        if not self.positions or LEVERAGE <= 0:
            return 0.0
        return sum(p.lot for p in self.positions) * CONTRACT * px / LEVERAGE

    def _mlevel(self, px: float) -> float:
        u = self._used(px)
        if u <= 0:
            return 999.0
        return (self.balance + self._floating(px)) / u

    def _can_add(self, px: float, lot: float) -> bool:
        # Mirrors the dashboard's canAdd(): cap, projected margin level after
        # the spread fee, and plain affordability of margin + fee out of free
        # equity. The affordability leg used to be missing here, so EC2 could
        # open a unit the dashboard backtest refused.
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

    def _active_sl(self) -> Optional[float]:
        """The stop that is actually protecting the run right now.

        The primary is stopped out well before the supplementaries in most
        runs. Reporting only the primary stop left `sl` null on 59% of the bars
        that had live tickets, so the chart trail line vanished and the bridge
        had nothing to modify. Fall back to the tightest live stop instead.
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
        self.pending = None

    def _flatten(self, px: float) -> str:
        self.balance += self._floating(px)
        self._clear_all()
        self.pending = None
        return "EXIT"

    def _close_one(self, p: Position, px: float) -> float:
        pnl = (px - p.entry if self.pos > 0 else p.entry - px) * CONTRACT * p.lot
        self.balance += pnl
        return pnl

    def _do_open(self, fill_px: float, want: int, is_primary: bool, lot: float, signal_bl: float) -> str:
        if not self._can_add(fill_px, lot):
            return "NONE"
        if is_primary:
            self.pos = want
            self.run_side = want
            self.seen_amber = False
            self.positions = [Position(entry=fill_px, sl=self._init_sl(fill_px, want), is_primary=True, lot=lot)]
            self._last_fill_lot = lot
            self.balance -= SPREAD_COST
            self.break_level = signal_bl
            return "BUY" if want > 0 else "SELL"
        if self.pos == 0:
            self.pos = self.run_side if self.run_side else want
        if self._n_supp() >= MAX_SUPP:
            return "HOLD"
        side = self.pos
        self.positions.append(
            Position(entry=fill_px, sl=self._init_sl(fill_px, side), is_primary=False, lot=lot)
        )
        self.break_level = signal_bl
        self._last_fill_lot = lot
        self.balance -= SPREAD_COST
        return "BUY_ADD" if side > 0 else "SELL_ADD"

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
        # Buy/add: THIS candle takes out the PREVIOUS body.
        if self.prev_open is None or self.prev_close is None:
            return False
        if want > 0:
            return h > body_high(self.prev_open, self.prev_close)
        return l < body_low(self.prev_open, self.prev_close)

    def _cross_fill(self, want: int, eo: float, eh: float | None = None, el: float | None = None) -> float:
        # Fill at previous body (or this open if already through).
        if want > 0:
            lvl = body_high(self.prev_open, self.prev_close)
            if eo > lvl:
                return eo
            tsl = effective_tsl_pts()
            if el is not None and abs(el - eo) < 1e-9 and eo < lvl - tsl:
                return eo
            return lvl
        lvl = body_low(self.prev_open, self.prev_close)
        if eo < lvl:
            return eo
        tsl = effective_tsl_pts()
        if eh is not None and abs(eh - eo) < 1e-9 and eo > lvl + tsl:
            return eo
        return lvl

    def _entry_ok(self, o: float, h: float, l: float, c: float, xt: float, want: int,
                  level: float, is_primary: bool = False) -> bool:
        # Primary: prev-body cross + X-Trend clear.
        # SUPP: only break last-trade candle wick (no XT gate).
        if is_primary:
            if not self._broke_prev_body(h, l, want):
                return False
            if XT_GATE and not self._xt_clear(h, l, xt, want):
                self.xt_skips += 1
                return False
            return True
        if level is None:
            return False
        if not self._broke_wick(h, l, float(level), want):
            return False
        # Optional: XTREND_GATE_SUPP=1 also demands the candle sit clear of X-Trend.
        if XT_GATE_SUPP and not self._xt_clear(h, l, xt, want):
            self.xt_skips += 1
            return False
        return True

    def _wick_fill(self, want: int, eo: float, level: float) -> float:
        if want > 0:
            return eo if eo > level else float(level)
        return eo if eo < level else float(level)

    def _open_on_cross(self, want: int, is_primary: bool, lot: float, h: float, l: float, eo: float) -> str:
        if is_primary:
            fill_px = self._cross_fill(want, eo, h, l)
        else:
            if self.break_level is None:
                return "NONE"
            fill_px = self._wick_fill(want, eo, float(self.break_level))
        sig_bl = h if want > 0 else l
        return self._do_open(fill_px, want, is_primary, lot, sig_bl)

    def _scan_stops(
        self,
        eh: float,
        el: float,
        eo: float,
        has_raw_execution: bool,
        only: list[Position] | None = None,
    ):
        sl_exits = 0
        primary_closed = False
        closed_primary: dict | None = None
        closed_supps: list[dict] = []
        if self._n_total() == 0:
            return sl_exits, primary_closed, closed_primary, closed_supps
        check_ids = None if only is None else {id(p) for p in only}
        survivors: list[Position] = []
        for p in self.positions:
            if check_ids is not None and id(p) not in check_ids:
                survivors.append(p)
                continue
            hit = (self.pos > 0 and el <= p.sl) or (self.pos < 0 and eh >= p.sl)
            if not hit:
                survivors.append(p)
                continue
            slipped_stop = (
                p.sl - STOP_SLIPPAGE_PTS
                if self.pos > 0
                else p.sl + STOP_SLIPPAGE_PTS
            )
            # Fill at the slipped stop. Do not take a worse gap-through open.
            stop_fill = slipped_stop
            pnl = self._close_one(p, stop_fill)
            sl_exits += 1
            rec = {
                "entry": p.entry, "exit": stop_fill, "lot": p.lot,
                "pnl": round(pnl, 4),
                "reason": "primary_tsl" if p.is_primary else "supp_tsl",
            }
            if p.is_primary:
                primary_closed = True
                closed_primary = rec
            else:
                closed_supps.append(rec)
        self.positions = survivors
        if not self.positions:
            self.pos = 0
        return sl_exits, primary_closed, closed_primary, closed_supps

    def push(
        self,
        o: float,
        h: float,
        l: float,
        c: float,
        xtrend: float,
        hist_in: float | None,
        histcolor: str | None = None,
        skip_worst_hours: bool | None = None,
        focus_best_hours: bool | None = None,
        time_str: str | None = None,
        raw_open: float | None = None,
        raw_high: float | None = None,
        raw_low: float | None = None,
        raw_close: float | None = None,
    ):
        raw_values = (raw_open, raw_high, raw_low, raw_close)
        if any(v is not None for v in raw_values) and not all(v is not None for v in raw_values):
            raise ValueError("raw_open, raw_high, raw_low and raw_close must be sent together")

        # Idempotency: a retried or duplicated bar replays its cached result and
        # never advances state twice. Checked after validation so a malformed
        # retry still errors instead of being served from cache.
        bar_key = str(time_str or "").strip()
        if bar_key and bar_key in self._seen_bars:
            duplicate = copy.deepcopy(self._seen_bars[bar_key])
            duplicate["duplicate_bar"] = True
            duplicate["dedupe_key"] = bar_key
            return duplicate

        has_raw_execution = all(v is not None for v in raw_values)
        self.last_execution_source = "raw_ohlc" if has_raw_execution else "signal_ohlc_fallback"
        eo, eh, el, ec = (
            (float(raw_open), float(raw_high), float(raw_low), float(raw_close))
            if has_raw_execution
            else (o, h, l, c)
        )
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
        action = "NONE"
        closed_supps: list[dict] = []
        closed_primary: dict | None = None
        primary_closed = False
        sl_exits = 0
        filled_action = None
        # FIX 3 — STOP: tickets opened on THIS bar go into just_opened and are
        # excluded from _scan_stops. That kills the −0.80 same-bar death when
        # open==low (buy) or open==high (sell). Stop only lives from NEXT bar.
        # See errors/…wrong_trade_fixes.md §5.
        just_opened: list[Position] = []
        sl_before = self._active_sl()

        if zz == 0:
            self.seen_amber = True
            self.pending = None

        if self.pending is not None:
            pa = self._do_open(
                eo, self.pending.want, self.pending.is_primary,
                self.pending.lot, self.pending.signal_break_level,
            )
            if pa in ("BUY", "SELL", "BUY_ADD", "SELL_ADD"):
                action = pa
                filled_action = pa
                just_opened.append(self.positions[-1])
            self.pending = None

        if self._n_total() > 0:
            # Only OLDER tickets can be stopped by this bar's wick.
            prior = [p for p in self.positions if all(p is not q for q in just_opened)]
            se, pc, cpri, csup = (
                self._scan_stops(eh, el, eo, has_raw_execution, only=prior)
                if prior else (0, False, None, [])
            )
            sl_exits += se
            if pc:
                primary_closed = True
                closed_primary = cpri
            closed_supps.extend(csup)
            if not self.positions:
                if sl_exits and filled_action is None:
                    action = "EXIT"
                elif action == "NONE":
                    action = "HOLD"

            if self._n_total() > 0:
                if zz == 0:
                    # FIX 4 — only amber hist flattens the run (not opposite HA colour).
                    action = self._flatten(ec)
                    self.run_side = 0
                    self.seen_amber = True
                else:
                    # FIX 4 — trail EVERY closed candle; do not freeze on opposite HA.
                    for p in self.positions:
                        p.sl = self._trail_sl(p.sl, ec, self.pos)

                    if (
                        zz == (1 if self.pos > 0 else -1)
                        and self._entry_ok(o, eh, el, ec, xtrend, self.pos, self.break_level)
                    ):
                        if not hour_allowed(hour, self.skip_worst_hours):
                            self.hour_skips += 1
                        else:
                            pa = self._open_on_cross(self.pos, False, lot, eh, el, eo)
                            if pa in ("BUY_ADD", "SELL_ADD"):
                                action = pa
                                filled_action = pa
                                just_opened.append(self.positions[-1])

                    if self._n_total() > 0 and self._mlevel(ec) < STOPOUT:
                        action = self._flatten(ec)
                        self.run_side = 0

        if (
            self._n_total() == 0
            and self.run_side != 0
            and zz == (1 if self.run_side > 0 else -1)
            and self._entry_ok(o, eh, el, ec, xtrend, self.run_side, self.break_level)
        ):
            if not hour_allowed(hour, self.skip_worst_hours):
                self.hour_skips += 1
            else:
                pa = self._open_on_cross(self.run_side, False, lot, eh, el, eo)
                if pa in ("BUY_ADD", "SELL_ADD"):
                    action = pa
                    filled_action = pa
                    just_opened.append(self.positions[-1])

        if zz == 0:
            # Orange/amber: flatten already done above; reset run + trailing levels.
            self.run_side = 0
            self.break_level = None
            self.pos = 0
            self.pending = None

        if (
            self._n_total() == 0
            and self.run_side == 0
            and self.seen_amber
            and self.pending is None
            and self.prev_high is not None
        ):
            want = 0
            if zz == 1 and self._entry_ok(o, h, l, c, xtrend, 1, self.prev_high, is_primary=True):
                want = 1
            elif zz == -1 and self._entry_ok(o, h, l, c, xtrend, -1, self.prev_low or l, is_primary=True):
                want = -1
            if want:
                if not hour_allowed(hour, self.skip_worst_hours):
                    self.hour_skips += 1
                else:
                    pa = self._open_on_cross(want, True, lot, eh, el, eo)
                    if pa in ("BUY", "SELL"):
                        action = pa
                        filled_action = pa
                        just_opened.append(self.positions[-1])

        if just_opened and self._n_total() > 0:
            # New tickets: trail to THIS close so NEXT bar uses close±0.25 as live SL.
            # Do not stop them against this bar's own wick (that wick made the trade).
            live_new = [p for p in just_opened if any(p is q for q in self.positions)]
            if ENTRY_BAR_MODE == "test" and live_new:
                # Risk and reward on the same bar: the initial stop is exposed to
                # this bar's extreme before it is allowed to trail to the close.
                se, pc, cpri, csup = self._scan_stops(
                    eh, el, eo, has_raw_execution, only=live_new
                )
                if se:
                    sl_exits += se
                    if pc:
                        primary_closed = True
                        closed_primary = cpri
                    closed_supps.extend(csup)
                    live_new = [p for p in live_new if any(p is q for q in self.positions)]
                    if not self.positions and action in ("BUY", "SELL", "BUY_ADD", "SELL_ADD"):
                        action = "EXIT"
            # Trail new tickets to THIS close in every entry-bar mode, including
            # 'defer'. Deferring must only skip the stop TEST on the entry bar —
            # skipping the trail too left the stop at entry±TSL, so the next bar
            # stopped the trade out for -(TSL+slippage)-spread even mid-trend.
            # _trail_sl only ever tightens toward profit, so this cannot widen risk.
            if TRAIL_ENTRY_BAR:
                for p in live_new:
                    p.sl = self._trail_sl(p.sl, ec, self.pos)

        if self._n_total() > 0 and self._mlevel(ec) < STOPOUT:
            action = self._flatten(ec)
            self.run_side = 0

        if action == "NONE":
            action = "HOLD" if self.pos else "NONE"

        self.prev_high = h
        self.prev_low = l
        self.prev_open = o
        self.prev_close = c

        active_sl = self._active_sl()
        fill_px = just_opened[-1].entry if just_opened else eo
        result = {
            "action": action,
            "hist": hist,
            "histcolor": color,
            "zone": zz,
            "ha_side": ha_side(o, c),
            "position": {0: "FLAT", 1: "LONG", -1: "SHORT"}[self.pos],
            "n_units": self._n_primary(),
            "n_supp": self._n_supp(),
            "n_total": self._n_total(),
            "sl": active_sl,
            "pending_next_open": self.pending is not None,
            "run_side": self.run_side,
            "seen_amber": self.seen_amber,
            "break_level": self.break_level,
            "supp_positions": [
                {"entry": p.entry, "sl": round(p.sl, 4), "lot": p.lot, "tsl_mode": "pts_025"}
                for p in self.positions if not p.is_primary
            ],
            "closed_supps": closed_supps,
            "closed_primary": closed_primary,
            "primary_closed": primary_closed,
            "sl_exits": sl_exits,
            "filled_action": filled_action,
            "fill_price": None if not just_opened else just_opened[-1].entry,
            "sl_updated": active_sl,
            "sl_changed": bool(
                active_sl is not None and (
                    sl_before is None or abs(active_sl - sl_before) > 1e-9
                )
            ),
            "open_positions": [
                {"entry": p.entry, "sl": round(p.sl, 4), "lot": p.lot, "is_primary": p.is_primary}
                for p in self.positions
            ],
            "skip_worst_hours": self.skip_worst_hours,
            "focus_best_hours": self.focus_best_hours,
            "best_lot_mult": BEST_LOT_MULT,
            "fill_lot": getattr(self, "_last_fill_lot", DEFAULT_VOLUME),
            "hour_utc4": hour,
            "hour_skips": self.hour_skips,
            "xt_skips": self.xt_skips,
            "entry_logic": "primary_add_body_xt_clear",
            "fill_mode": "prev_body_immediate",
            "close_confirm": False,
            "tsl_pts": effective_tsl_pts(),
            "mode": "playbook_a_body_xt_clear_025pt",
            "xtrend": xtrend,
            "xtrend_gate": XT_GATE,
            "xtrend_gate_supp": XT_GATE_SUPP,
            "xtrend_touch_buf": XT_BUF,
            "primary_wick_gate": PRIMARY_WICK_GATE,
            "stop_slippage_pts": STOP_SLIPPAGE_PTS,
            "stop_active_on_entry_bar": ENTRY_BAR_MODE == "test",
            "entry_bar_mode": ENTRY_BAR_MODE,
            "trail_every_candle": True,
            "execution_price_source": "raw_ohlc" if has_raw_execution else "signal_ohlc_fallback",
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "raw_open": eo,
            "raw_high": eh,
            "raw_low": el,
            "raw_close": ec,
            "spread_cost": SPREAD_COST,
            "balance": round(self.balance, 4),
            "duplicate_bar": False,
            "dedupe_active": bool(bar_key),
            "closed_units": (
                ([{"kind": "primary", **closed_primary}] if closed_primary else [])
                + [{"kind": "supp", **x} for x in closed_supps]
            ),
            "fill_sl": (
                None if filled_action is None else self._init_sl(
                    fill_px, 1 if "BUY" in filled_action else -1
                )
            ),
            "position_stops": [
                {"entry": p.entry, "sl": round(p.sl, 4), "lot": p.lot, "is_primary": p.is_primary}
                for p in self.positions
            ],
        }
        if bar_key:
            self._seen_bars[bar_key] = copy.deepcopy(result)
            self._seen_bars.move_to_end(bar_key)
            while len(self._seen_bars) > MAX_SEEN_BARS:
                self._seen_bars.popitem(last=False)
        return result


engine = LatestModsEngine()
DocsRulesEngine = LatestModsEngine
# /signal, /warmup and /reset all mutate one engine. Uvicorn serves requests on
# a thread pool, so concurrent bars must not interleave inside push().
engine_lock = threading.RLock()


def _save_engine_state() -> None:
    """Atomically persist state; a crash cannot leave a half-written snapshot."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(engine.snapshot(), separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception:
        # Persistence is best-effort: a read-only or missing volume must never
        # take the trading endpoint down.
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
app = FastAPI(title=f"XAUUSD {MODEL_LABEL}", version="7.0-asim-hist-rules")


def _order_hint(res: dict) -> Optional[dict]:
    # A replayed duplicate must never re-issue broker orders.
    if res.get("duplicate_bar"):
        return None
    orders: list[dict] = []
    vol = float(res.get("fill_lot") or DEFAULT_VOLUME)
    sl = res.get("sl")
    fill_sl = res.get("fill_sl")
    active_sl = res.get("sl_updated")
    broker_sl = active_sl if active_sl is not None else (fill_sl if fill_sl is not None else sl)
    filled = res.get("filled_action") or (
        res["action"] if res["action"] in ("BUY", "SELL", "BUY_ADD", "SELL_ADD") else None
    )
    if filled in ("BUY", "BUY_ADD"):
        orders.append({
            "actionType": "ORDER_TYPE_BUY", "symbol": SYMBOL, "volume": vol,
            "sl": broker_sl, "stopLoss": broker_sl,
        })
    elif filled in ("SELL", "SELL_ADD"):
        orders.append({
            "actionType": "ORDER_TYPE_SELL", "symbol": SYMBOL, "volume": vol,
            "sl": broker_sl, "stopLoss": broker_sl,
        })
    if res["action"] == "EXIT" or (res.get("sl_exits") and res.get("n_total", 1) == 0):
        orders.append({"actionType": "POSITIONS_CLOSE_SYMBOL", "symbol": SYMBOL})
    else:
        # Partial stop-outs: some tickets closed, others still live. Without
        # these the broker would keep running positions the engine has already
        # booked as closed.
        for closed in ([res["closed_primary"]] if res.get("closed_primary") else []) + list(
            res.get("closed_supps") or []
        ):
            orders.append({
                "actionType": "POSITIONS_CLOSE_PARTIAL_SYMBOL",
                "symbol": SYMBOL,
                "volume": float(closed.get("lot") or DEFAULT_VOLUME),
                "entry": closed.get("entry"),
                "exit": closed.get("exit"),
                "reason": closed.get("reason"),
            })
    if res.get("sl_changed") and active_sl is not None and res.get("n_total", 0) > 0:
        orders.append({
            "actionType": "SL_MODIFY", "symbol": SYMBOL,
            "sl": active_sl, "stopLoss": active_sl,
            # Per-ticket stops: a single symbol-level SL cannot express
            # per-position trailing once supplementaries are open.
            "positions": res.get("open_positions") or [],
        })
    if not orders:
        return None
    if len(orders) == 1:
        return orders[0]
    return {"orders": orders}


@app.get("/health")
def health():
    with engine_lock:
        return {
        "ok": True,
        "model": MODEL_LABEL,
        "mode": "playbook_a_body_xt_clear_025pt",
        "warmed_up": engine.warmed,
        "position": {0: "FLAT", 1: "LONG", -1: "SHORT"}[engine.pos],
        "n_units": engine._n_primary(),
        "n_supp": engine._n_supp(),
        "n_total": engine._n_total(),
        "sl": None if not engine.positions else next(
            (p.sl for p in engine.positions if p.is_primary), engine.positions[0].sl
        ),
        "entry_logic": "primary_add_body_xt_clear",
        "fill_mode": "prev_body_immediate",
        "close_confirm": False,
        "tsl_pts": effective_tsl_pts(),
        "xtrend_gate": XT_GATE,
        "xtrend_gate_supp": XT_GATE_SUPP,
        "xtrend_touch_buf": XT_BUF,
        "primary_wick_gate": PRIMARY_WICK_GATE,
        "stop_slippage_pts": STOP_SLIPPAGE_PTS,
        "stop_active_on_entry_bar": ENTRY_BAR_MODE == "test",
        "entry_bar_mode": ENTRY_BAR_MODE,
        "trail_every_candle": True,
        "raw_execution_supported": True,
        "last_execution_source": engine.last_execution_source,
        "raw_execution_active": engine.last_execution_source == "raw_ohlc",
        "spread_cost": SPREAD_COST,
        "balance": round(engine.balance, 4),
        "bar_dedupe": f"locked_persistent_timestamp_cache_{MAX_SEEN_BARS}",
        "dedupe_cached_bars": len(engine._seen_bars),
        "state_persistence": str(STATE_FILE),
        "state_restored_on_boot": STATE_RESTORED,
        "xt_skips": engine.xt_skips,
        "skip_worst_hours": engine.skip_worst_hours,
        "focus_best_hours": engine.focus_best_hours,
        "best_lot_mult": BEST_LOT_MULT,
        "pending_next_open": engine.pending is not None,
        "engine_family": "playbook_a_prev_body_xt_clear_025pt",
        "partial_exit_orders": True,
        "entry_bar_stop_test": ENTRY_BAR_MODE == "test",
        }


@app.post("/reset")
def reset(x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    with engine_lock:
        engine.reset()
        _save_engine_state()
    return {"ok": True}


@app.post("/warmup")
def warmup(req: WarmupReq, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    _validate_raw_group(req.candles)
    with engine_lock:
        engine.reset()
        last = None
        for c in req.candles:
            last = engine.push(
                c.open, c.high, c.low, c.close, c.xtrend, c.hist, c.histcolor,
                skip_worst_hours=c.skip_worst_hours,
                focus_best_hours=c.focus_best_hours,
                time_str=c.time,
                raw_open=c.raw_open,
                raw_high=c.raw_high,
                raw_low=c.raw_low,
                raw_close=c.raw_close,
            )
        engine.pos = 0
        engine.positions = []
        engine.break_level = None
        engine.run_side = 0
        engine.pending = None
        engine.balance = START_BALANCE
        # Replay-only diagnostics must not be reported as live skips.
        engine.hour_skips = 0
        engine.xt_skips = 0
        # Warmup bars are replay, not live bars: clear the dedupe cache so the
        # same timestamps arriving live are processed rather than served stale.
        engine._seen_bars.clear()
        _save_engine_state()
    return {
        "ok": True,
        "fed": len(req.candles),
        "warmed_up": engine.warmed,
        "position": "FLAT",
        "n_units": 0,
        "mode": "playbook_a_body_xt_clear_025pt",
        "last_action": None,
        "warmup_last_action": None if last is None else last.get("action"),
        "trade_state_cleared": True,
        "balance_reset": True,
        "balance": engine.balance,
    }


@app.post("/signal")
def signal(c: Candle, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    _validate_raw_group([c])
    with engine_lock:
        res = engine.push(
            c.open, c.high, c.low, c.close, c.xtrend, c.hist, c.histcolor,
            skip_worst_hours=c.skip_worst_hours,
            focus_best_hours=c.focus_best_hours,
            time_str=c.time,
            raw_open=c.raw_open,
            raw_high=c.raw_high,
            raw_low=c.raw_low,
            raw_close=c.raw_close,
        )
        res["time"] = c.time
        res["order"] = _order_hint(res)
        res["send_order"] = res["order"] is not None
        # A duplicate changed nothing; rewriting state would only churn the disk.
        if not res.get("duplicate_bar"):
            _save_engine_state()
    return res
