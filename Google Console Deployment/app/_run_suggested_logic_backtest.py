"""Backtest suggested live logic: filters OFF, 0.01 lot, gaga XT, defer entry-bar."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "XAUUSDm" / "XAUUSDm_M15_202601012300_202608271930.csv"
ENGINE = ROOT / "Ali PC Deployment" / "runtime" / "mt5_live_engine.py"
BRIDGE = ROOT / "Ali PC Deployment" / "runtime" / "bridge_trader.py"
OUT = Path(__file__).resolve().parent / "suggested_logic_backtest_results.json"


def load_module(path: Path, name: str):
    # Stub MetaTrader5 for bridge import
    if "MetaTrader5" not in sys.modules:
        mt5 = types.ModuleType("MetaTrader5")
        mt5.TRADE_RETCODE_DONE = 10009
        sys.modules["MetaTrader5"] = mt5
    runtime = str(path.parent)
    if runtime not in sys.path:
        sys.path.insert(0, runtime)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_rows():
    import csv

    rows = []
    with DATA.open("r", encoding="utf-8-sig", newline="") as f:
        for raw in csv.DictReader(f, delimiter="\t"):
            try:
                dt = datetime.strptime(
                    f"{raw['<DATE>']} {raw['<TIME>']}", "%Y.%m.%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                rows.append(
                    {
                        "time": dt.isoformat().replace("+00:00", "Z"),
                        "dt": dt,
                        "open": float(raw["<OPEN>"]),
                        "high": float(raw["<HIGH>"]),
                        "low": float(raw["<LOW>"]),
                        "close": float(raw["<CLOSE>"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
    return rows


def prepare_bars(rows, bridge):
    opens = [r["open"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    closes = [r["close"] for r in rows]
    ha_o, ha_c, ha_h, ha_l = bridge._heiken_ashi(opens, highs, lows, closes)
    rsi = bridge._wilder_rsi(closes, 3)
    hist = bridge._ema_span(rsi, 5)
    xtrend, xt_src = bridge._compute_xtrend(opens, highs, lows, closes, ha_h, ha_l, ha_c)
    bars = []
    for i, row in enumerate(rows):
        value = hist[i] - 50.0
        bars.append(
            {
                **row,
                "raw_open": row["open"],
                "raw_high": row["high"],
                "raw_low": row["low"],
                "raw_close": row["close"],
                "signal_open": ha_o[i],
                "signal_high": ha_h[i],
                "signal_low": ha_l[i],
                "signal_close": ha_c[i],
                "hist": round(value, 2),
                "histcolor": bridge.hist_color(value),
                "xtrend": round(float(xtrend[i]), 3),
                "xtrend_source": xt_src,
            }
        )
    return bars


def closed_events(before, before_side, result, row, engine_mod):
    events = []
    accounted = set()
    if result.get("closed_primary"):
        unit = result["closed_primary"]
        events.append({"kind": "PRIMARY", **unit})
        accounted.add((round(float(unit["entry"]), 8), round(float(unit["lot"]), 8)))
    for unit in result.get("closed_supps") or []:
        events.append({"kind": "SUPP", **unit})
        accounted.add((round(float(unit["entry"]), 8), round(float(unit["lot"]), 8)))
    if before and not result.get("open_positions"):
        reason = "orange" if result.get("zone") == 0 else "margin_stop"
        for position in before:
            key = (round(float(position.entry), 8), round(float(position.lot), 8))
            if key in accounted:
                continue
            pnl = (
                (float(row["close"]) - float(position.entry))
                if before_side > 0
                else (float(position.entry) - float(row["close"]))
            ) * engine_mod.CONTRACT * float(position.lot)
            events.append(
                {
                    "kind": "PRIMARY" if position.is_primary else "SUPP",
                    "entry": float(position.entry),
                    "exit": float(row["close"]),
                    "lot": float(position.lot),
                    "pnl": round(pnl, 6),
                    "reason": reason,
                }
            )
    return events


def run_case(bars, engine_mod, start_balance: float) -> dict:
    engine = engine_mod.Mt5LiveEngine()
    inner = engine.engine
    inner.balance = float(start_balance)
    all_events = []
    primary_entries = 0
    supp_entries = 0
    exit_reasons = Counter()
    peak = float(start_balance)
    max_dd = 0.0

    for row in bars:
        before = list(inner.positions)
        before_side = int(inner.pos)
        result = engine.push(row)
        filled = result.get("filled_action")
        if filled in ("BUY", "SELL"):
            primary_entries += 1
        elif filled in ("BUY_ADD", "SELL_ADD"):
            supp_entries += 1
        events = closed_events(before, before_side, result, row, engine_mod)
        all_events.extend(events)
        for event in events:
            exit_reasons[event.get("reason") or "unknown"] += 1
        equity = float(inner.balance + inner._floating(float(row["close"])))
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    final_equity = float(inner.balance + inner._floating(float(bars[-1]["close"])))
    wins = sum(1 for e in all_events if float(e["pnl"]) > 0)
    losses = len(all_events) - wins
    gross_profit = sum(float(e["pnl"]) for e in all_events if float(e["pnl"]) > 0)
    gross_loss = sum(abs(float(e["pnl"])) for e in all_events if float(e["pnl"]) <= 0)
    pnls = [float(e["pnl"]) for e in all_events]
    best_trade = max(pnls) if pnls else 0.0
    worst_trade = min(pnls) if pnls else 0.0
    best_event = max(all_events, key=lambda e: float(e["pnl"])) if all_events else None
    worst_event = min(all_events, key=lambda e: float(e["pnl"])) if all_events else None
    return {
        "start_balance": start_balance,
        "final_equity": round(final_equity, 2),
        "net_pnl": round(final_equity - start_balance, 2),
        "closed_trades": len(all_events),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100 * wins / len(all_events), 2) if all_events else 0.0,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_drawdown": round(max_dd, 2),
        "primary_entries": primary_entries,
        "supp_entries": supp_entries,
        "exit_reasons": dict(exit_reasons),
        "avg_win": round(gross_profit / wins, 4) if wins else 0.0,
        "avg_loss": round(gross_loss / losses, 4) if losses else 0.0,
        "best_trade_pnl": round(best_trade, 2),
        "worst_trade_pnl": round(worst_trade, 2),
        "best_trade": {
            "kind": best_event.get("kind"),
            "pnl": round(float(best_event["pnl"]), 2),
            "entry": best_event.get("entry"),
            "exit": best_event.get("exit"),
            "lot": best_event.get("lot"),
            "reason": best_event.get("reason"),
        }
        if best_event
        else None,
        "worst_trade": {
            "kind": worst_event.get("kind"),
            "pnl": round(float(worst_event["pnl"]), 2),
            "entry": worst_event.get("entry"),
            "exit": worst_event.get("exit"),
            "lot": worst_event.get("lot"),
            "reason": worst_event.get("reason"),
        }
        if worst_event
        else None,
    }


def main():
    if not DATA.is_file():
        raise SystemExit(f"missing data: {DATA}")

    base_env = {
        "VOLUME": "0.01",
        "SKIP_WORST_HOURS": "0",
        "FOCUS_BEST_HOURS": "0",
        "SKIP_WEEKENDS": "1",
        "XTREND_GATE": "1",
        "XTREND_GATE_SUPP": "0",
        "XTREND_BUF": "0",
        "XTREND_SOURCE": "gaga",
        "TRAIL_EVERY_CANDLE": "1",
        "TRAIL_ENTRY_BAR": "1",
        "ENTRY_BAR_MODE": "defer",
        "STOP_SLIPPAGE_PTS": "0.25",
        "SPREAD_COST": "0.06",
        "DISABLE_STOP_LOSS": "0",
        "PRIMARY_WICK_GATE": "0",
        "BEST_LOT_MULT": "1",
        "START_BALANCE": "1000",
    }
    os.environ.update(base_env)

    bridge = load_module(BRIDGE, "bt_bridge_ali")
    rows = load_rows()
    bars = prepare_bars(rows, bridge)
    print(
        f"bars={len(bars)} from={bars[0]['time']} to={bars[-1]['time']} "
        f"xt={bars[0].get('xtrend_source')}"
    )

    # Suggested logic TSL=3, plus current live TSL=1 and playbook 0.25 for context
    cases = [
        ("suggested_tsl_3", "3"),
        ("current_live_tsl_1", "1"),
        ("playbook_tsl_0_25", "0.25"),
        ("wider_tsl_5", "5"),
    ]
    results = {
        "data": str(DATA),
        "bars": len(bars),
        "from": bars[0]["time"],
        "to": bars[-1]["time"],
        "common": {
            "VOLUME": 0.01,
            "SKIP_WORST_HOURS": 0,
            "FOCUS_BEST_HOURS": 0,
            "XTREND_SOURCE": "gaga",
            "ENTRY_BAR_MODE": "defer",
            "TRAIL_EVERY_CANDLE": 1,
            "TRAIL_ENTRY_BAR": 1,
            "START_BALANCE": 1000,
        },
        "cases": {},
    }

    for name, tsl in cases:
        os.environ["TSL_PTS"] = tsl
        # Reload engine so module-level defaults refresh where needed
        for key in list(sys.modules):
            if key.startswith("bt_engine_"):
                del sys.modules[key]
        engine_mod = load_module(ENGINE, f"bt_engine_{name}")
        # Ensure env-driven helpers see new TSL
        if hasattr(engine_mod, "TSL_PTS"):
            engine_mod.TSL_PTS = float(tsl)
        stats = run_case(bars, engine_mod, 1000.0)
        stats["TSL_PTS"] = float(tsl)
        results["cases"][name] = stats
        print(
            f"{name}: net={stats['net_pnl']} wr={stats['win_rate_pct']}% "
            f"trades={stats['closed_trades']} pf={stats['profit_factor']} "
            f"dd={stats['max_drawdown']} best={stats['best_trade_pnl']} "
            f"worst={stats['worst_trade_pnl']}"
        )

    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
