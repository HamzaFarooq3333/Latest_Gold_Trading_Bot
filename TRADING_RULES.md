# Live Trading Rules â€” Ali desk, current as of 2026-09-13

> **Source of truth for behaviour:** `Ali PC Deployment/runtime/mt5_live_engine.py`
> (byte-identical copy in `Google Console Deployment/app/mt5_live_engine.py`).
> Every rule below is enforced by that file and covered by
> `Ali PC Deployment/runtime/test_engine_rules.py`.

---

## 1. Inputs (closed M15 candles from the Ali PC MT5 terminal)

| Series | Used for |
|--------|----------|
| **Heikin-Ashi OHLC** | previous-body break, X-Trend clearance |
| **Raw broker OHLC** | fills, stops, ATR |
| `hist = EMA(WilderRSI(raw close, 3), 5) âˆ’ 50` | colour |
| X-Trend = **KJ GagaTrend on HA** (`XTREND_SOURCE=gaga`) | clearance gate |

Colour (`HIST_THRESH`, live **10**): green `hist ≥ +10`, red `hist ≤ −10`, amber between.
The bridge colours each bar with the live threshold and the engine trusts that colour.

## 2. Entries â€” live mode (`ENTRY_EVERY_CANDLE=1`)

On **every** green (red) candle:

1. this candle's HA **high > previous HA body high** (low < body low for sells), and
2. the whole candle sits **clear of X-Trend**: `low > XT` (buy) / `high < XT` (sell), buffer 0,

â†’ open one ticket at the previous body level (or this raw open if it gapped through; live: current ask/bid).
Tickets **stack in the run direction** up to `MAXPOS=20`; an opposite-colour candle while tickets are open does nothing.
No amber arming, no supplementary wick logic (`MAX_SUPP=0`).

Same-bar sequencing: stops are tested first, then the entry gate â€” a bar that stops the run out and passes the gate re-enters on that bar.

Classic mode (`ENTRY_EVERY_CANDLE=0`, not live) keeps the old rules: amber arms, one primary per colour run, SUPP on the raw wick of the last trade candle, `MAX_SUPP`.

## 3. Stops

| Setting | Live value |
|---------|------------|
| Stop distance | **1.25 Ã— Wilder ATR(14)** of raw M15 bars at entry (`TSL_ATR_MULT`), fixed for that ticket |
| Fallback | `TSL_TICKS=1111` Ã— `TSL_TICK_SIZE=0.001` = $1.111 when the multiplier is 0 |
| Trail | after every closed candle each ticket's stop ratchets to `close âˆ“ distance`; never loosens |
| Entry bar | not stop-tested (`ENTRY_BAR_MODE=defer`) but its close does trail the stop (`TRAIL_ENTRY_BAR=1`) |
| Broker side | the submitted SL is only widened to the broker's minimum stop distance; the engine's stop is unchanged |
| Take-profit | none |

## 4. Exits

* per-ticket trailing stop hit (`raw low â‰¤ SL` for longs / `raw high â‰¥ SL` for shorts)
* **amber histogram flattens everything** and resets the run
* margin level below 50 % flattens

## 5. Risk / sizing

| Setting | Value |
|---------|-------|
| Lot (`VOLUME`) | **0.02** |
| Leverage | 1:100 |
| Add blocked if projected margin level < 75 % | yes |
| Max tickets | 20 |
| Hour / weekend filters | **OFF** |

## 6. Configuration precedence

`C:\onyxion-ali\.env` is the source of truth. The desk's *Live controls* override it **only after someone presses Save on the dashboard** (`updated_at` set). A desk reset or redeploy hands back untouched defaults and changes nothing on the PC.

## 7. Broker reconciliation

Every entry is bound to its MT5 position ticket. Each poll the bridge drops engine tickets the broker no longer holds (its stop fired), modifies stops per ticket, and never closes "the nearest" position by price.

## 8. Desks

| Desk | VM | URL | X-Trend |
|------|-----|-----|---------|
| Ali | `instance-20260831-171822` | https://35.253.21.246 | gaga |
| Hamza | `hamzatestserver01` | https://35.232.76.12 | supertrend |

---

## Lessons kept (do not repeat)

1. **A hard-coded histogram threshold in the bridge** coloured bars at 10 while `.env` said 15 â€” every tunable is now read live from the environment (`effective_*`), including `hist_color`.
2. **Trailing the bar extreme** put stops on the wrong side of the market and inflated backtests 25Ã—; trail the **close**.
3. **Sending the run's active stop with a stacked add** made the broker close the add before the engine knew â€” each ticket sends its own `fill_sl`.
4. **Partial close by nearest entry** could close a different live ticket â€” match by ticket, and treat "no matching ticket" as already closed by the broker.
5. **Desk controls overriding `.env` every 2 s** silently reset lot / threshold / SUPP on each redeploy â€” `.env` wins unless saved on the desk.
6. `auto_update_bridge.ps1` never actually restarted the bridge (`[regex]::Escape` + `-like`); the connection monitor's stale-code check was the only thing that did.
7. Backtests must stop-test the entry bar pessimistically and charge spread; the desk backtester now runs the same every-candle / ATR rules as live.
8. Never optimise the live threshold on an 11-day window (TH 20 won the window, lost 30 % out-of-sample over 6 months).
