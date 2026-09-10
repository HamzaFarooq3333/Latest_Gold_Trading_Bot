# Live Trading Rules (Playbook A) — current as of 2026-09-05

> **Source of truth for behaviour:** `Google Console Deployment/app/server.py`  
> **Deploy with:** `python deploy_to_live.py` (this folder)  
> After any big rule / path / deploy change: update **this file**, `PATHS.md`, and `deploy_to_live.py`.

---

## 1. Histogram

```
hist = EMA(WilderRSI(close, 3), 5) − 50
```

| Colour | Condition | Action |
|--------|-----------|--------|
| **Green** | `hist ≥ +10` | Buy / add when this candle crosses the **previous body** |
| **Orange** | `−10 < hist < +10` | No new trade · **flatten** if in a position · reset trail / run |
| **Red** | `hist ≤ −10` | Sell / add when this candle crosses the **previous body low** |

- Must see amber/orange at least once after flat before the next primary (`seen_amber`).
- One primary per colour run; after that only adds until orange.

## 2. Entry

**One new entry (primary or SUPP) per bar max.**

### Primary
- Previous-**body** cross on the signal candle:  
  - Buy: `high > max(prev_open, prev_close)`  
  - Sell: `low < min(prev_open, prev_close)`
- Fill at previous body level (or this bar’s **raw** open if already through / gap).
- Must clear X-Trend (see §3).

### Supplementary (SUPP / add)
- **Not** a previous-body cross. Only a **raw wick break of the last trade candle**:  
  - Buy SUPP: `raw_high > last_trade_raw_high`  
  - Sell SUPP: `raw_low < last_trade_raw_low`
- Fill at that raw wick level (or this raw open if already through).
- Same-side histogram colour required; X-Trend gate for SUPP is **optional** (default **OFF**).

> Heikin Ashi is the **signal** series (hist / body-break / XT). Fills, stops, and SUPP wick levels use **raw MT5 OHLC**. Trading view defaults to raw candles so arrow P/L matches the bar you see.

## 3. X-Trend gate

Candle must **NOT** touch the X-Trend line (buffer default `0.00`):

- Buy: `low > X-Trend + buffer`
- Sell: `high < X-Trend − buffer`
- Wick on the line → **skip**

| Gate | Env / UI | Default |
|------|----------|---------|
| **Primary** | `XTREND_GATE` / Testing “X-Trend gate (primary)” | **ON** |
| **SUPP** | `XTREND_GATE_SUPP` / Testing “X-Trend gate (SUPP)” · live controls | **OFF** (wick break only) |

## 4. Stops / trail

| Setting | Live value |
|---------|------------|
| `TSL_PTS` | **0.25** |
| Trail | Every closed 15m candle, ratchet only in favour |
| Buy SL | `price − 0.25` |
| Sell SL | `price + 0.25` |
| Take-profit | None |
| `ENTRY_BAR_MODE` | `defer` — entry bar is **not stop-tested** (the wick that filled must not kill the ticket) |
| `TRAIL_ENTRY_BAR` | **1 (ON)** — entry bar’s **close still trails** the stop. With this OFF the stop stays at entry ± TSL into the next bar and any retrace closes for exactly `−(TSL + slippage) − spread` (the repeated **−0.56**) |
| `STOP_SLIPPAGE_PTS` | **0.25** (fill at slipped stop, not gap-open; kept pessimistic by choice) |

## 5. Risk / sizing

| Setting | Value |
|---------|-------|
| Lot | 0.01 |
| Leverage | 1:100 |
| Block add if margin level would fall under ~75% | Yes |
| Max positions / max supp | 20 / 10 |
| `SKIP_WEEKENDS` | **0** (live off — engine + bridge evaluate weekend bars; broker may still reject if market closed) |

## 6. Desks

| Desk | VM | URL | Auth | Notes |
|------|-----|-----|------|-------|
| Ali | `instance-20260831-171822` | https://35.223.235.204 | ali / 123451 | **X-Trend = KJ GagaTrend** (`XTREND_SOURCE=gaga`) |
| Hamza | `hamzatestserver01` | https://35.232.76.12 | hamza / 123451 | **X-Trend = SuperTrend HA 6/0.8** (`XTREND_SOURCE=supertrend`) |

Remote app dir: `/opt/asim-gcp/` · service: `asim-gcp`

---

## Mistakes / lessons (do not repeat)

### Stops and fills
1. **`TSL_PTS=0.25` looks great on 15m bar backtests but dies live** — XAUUSD median spread ~2.6; same-bar dip hits 0.25 ~95% of the time. Live often stopped in 3–15 seconds. Wider stops (3 / ~7 ATR) survive noise but change PF — only change with explicit user OK.
2. **Gap-open stop fill was a silent loss source** — filling at `min/max(slipped_stop, bar_open)` turned entry-bar trails into fake huge losses. Live now uses **`stop_fill = slipped_stop` only**.
3. **`ENTRY_BAR_MODE=defer` must only skip the stop TEST on the entry bar** — not the trail. Old code also skipped trailing (`ENTRY_BAR_MODE != defer` / `!= 'defer'`), so the stop stayed at entry ± TSL; next bar’s stop check ran *before* trail and closed winners for exactly **`−(0.25+0.25)−0.06 = −0.56`**. Fix: **`TRAIL_ENTRY_BAR=1`** (default ON) — trail entry-bar close in every mode; toggle in Testing / live controls for A/B.
4. **Reporting only the primary’s SL** left `sl: null` on ~59% of bars with open tickets after primary stopped out → chart trail vanished and bridge skipped `SL_MODIFY`. Fix: **`_active_sl()`** = primary SL or tightest live stop.

### Rule confusion (costly)
5. **Touch vs clear** — user screenshots + “touch” wording led to deploying XT **touch**; playbook text says **must NOT touch (clear)**. Always confirm against the written playbook before flipping the gate.
6. **Primary vs SUPP entry are different** — primary = prev-body + XT clear; SUPP = **raw wick of last trade candle only** (XT optional via `XTREND_GATE_SUPP`, default OFF). Do not put body-break + XT on SUPP unless the user turns that gate ON.
7. **TSL 7 vs 0.25** — oscillated after volatility analysis; playbook A = **0.25**. Do not “optimize” live TSL without an explicit choice.

### Chart / P/L disputes (2026-09-05)
8. **Tall green HA candle + SUPP +0.05 / −0.56 is usually not a math bug** — Trading view used to paint **Heikin Ashi**; fills/stops use **raw**. HA open lags, so a 3–4 pt painted body can be a **&lt;1 pt raw** bar. SUPP often fills at the previous raw wick (already near the top of the real move). Arrow P/L is the closed ticket, not the painted body. Fix: chart defaults to **raw MT5 OHLC**; toggle HA if needed; click-bar panel shows HA body vs raw body.
9. **Backtest SUPP used HA high/low for wick break while live used raw** — lab fired SUPP on every HA new high in a trend, then died on the raw 0.25 stop. Fix: **`brokeWick` / `breakLevel` / fill use `rawHigh` / `rawLow`**, matching live.
10. **Chart markers** — arrows only, with close P/L on the entry arrow; **no EXIT circles**. Blue/purple dots on XT/SL were line **last-value** markers — turn `lastValueVisible` / `priceLineVisible` off on those series.
11. **One entry per bar** — at most one new primary or SUPP fill per bar (`openedBars` / `just_opened`); older lots can still exit on that bar.

### Infrastructure
12. **Dashboard “not loading” was often firewall IP drift**, not cache — rule `allow-asim-live-443`; public IP changes → add `/32`. `deploy_to_live.py` refreshes this.
13. **Hamza disk full (`ENOSPC`)** — controls POST / state writes fail. Truncate ops-agent + syslog; `/` must have free space.
14. **Windows MT5 VMs often refuse gcloud SSH keys** — prefer Linux desk deploy + bridge auto-update / controls pull; don’t assume Windows SCP works.
15. **gcloud auth expires** — keep `CLOUDSDK_CORE_DISABLE_PROMPTS=0` (User env). Helper: `ensure_gcloud_auth.ps1`. Local durable config: `%USERPROFILE%\.config\gcloud\onyxion-gcloud.env.ps1` (never commit). Deploy scripts call the helper automatically.
16. **Preserve `AUTH_SECRET` on `.env` overwrite** — `remote_install_best_engine.sh` already does this; don’t wipe sessions.

### Indicator / feed
17. **SuperTrend ≠ chart X-Trend** — TradingView CSV matched **KJ GagaTrend** far better; **Ali = `XTREND_SOURCE=gaga` (KJ)**, **Hamza = `supertrend`**. Live chart shows `XT=gaga_ha` vs `XT=supertrend_ha_6_0.8`.
18. **Signal OHLC may be Heikin-Ashi; execution must use raw OHLC** when provided — mismatches cause “should have been a big profit” disputes (see § Chart / P/L disputes).
19. **Charts look “stuck” on weekends** — XAUUSD M15 has no new bars while the market is closed; bridge still heartbeats. UI labels **WEEKEND / STALE**. Hard-refresh `/live` (Ctrl+F5) after deploys.
20. **Bridge auto-update can lag for days** — publish alone is not enough; force `auto_update_bridge.ps1 -Force` on Windows and restart so `XTREND_SOURCE` is applied.
21. **Backtest X-Trend source is selectable** (SuperTrend HA vs KJ GagaTrend vs CSV) via `dashboard.html` — deploy with **`python deploy_backtest_only.py`** so live is never touched. Working restore: `snapshots/working_2026-09-05/`.

### Process
22. After any big live change: update **`TRADING_RULES.md`**, **`PATHS.md`**, and **`deploy_to_live.py`** in the same turn.
23. Push live **and** backtest UI only via **`python deploy_to_live.py`** (deploys `server.py` + `static/dashboard.html`). Hard-refresh the browser after deploy.
24. **Backtest page (`/`) is a separate JS engine** in `dashboard.html` — it must be deployed with live or the site backtest drifts from Playbook A.

---

## Fixes logged 2026-09-05 (do not regress)

| Fix | What was wrong | What is correct now |
|-----|----------------|---------------------|
| **Entry-bar trail** | `defer` skipped trail → uniform **−0.56** stop-outs mid-trend | `TRAIL_ENTRY_BAR=1`: trail entry-bar close; still no stop-*test* on entry bar |
| **SUPP entry** | Treated like primary (body + XT) or used HA wick | Raw wick of last trade candle only; fill at that raw wick |
| **SUPP XT toggle** | No separate control | `XTREND_GATE_SUPP` / Testing “X-Trend gate (SUPP)” — default **OFF** |
| **HA vs raw chart** | Tall HA candle made SUPP **+0.05** look like a missed winner | Trading view defaults to **raw MT5**; HA optional; bar panel shows both bodies |
| **Backtest wick = HA** | Lab SUPP on HA highs, live on raw | Backtest `brokeWick` / `breakLevel` use **rawHigh/rawLow** |
| **Markers** | EXIT circles + PnL confusion; line-end dots on XT/SL | Arrows only with close PnL; no EXIT circles; XT/SL last-value markers off |
| **One entry / bar** | Multiple opens on one candle | Max one new primary or SUPP fill per bar |
| **STOP_SLIPPAGE_PTS** | Backtest showed 0.05 ≈ +$1k vs 0.25 | Left at **0.25** by explicit choice (pessimistic) |
