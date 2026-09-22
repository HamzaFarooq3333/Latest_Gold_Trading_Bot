# CONTEXT.md — running memory of the Onyxion gold bot work with Ali

> Read this first at the start of every chat. Update it at the end of every
> session (append to the log, refresh the "Current state" section). Newest
> entries at the bottom of each section. No passwords or tokens in here, ever.

---

## 1. Hard rules from Ali (never break)

- Do NOT ask for, store, or use Google / GCP passwords. Do not invent credentials.
- Do NOT run gcloud, install.sh, SSH to GCP, or redeploy any cloud dashboard from this PC.
- Do NOT flatten, close, or cancel open MT5 positions for updates. Never flatten trades.
- Source of truth repo ONLY: https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot
- Live desk URL (view/status only): https://35.253.21.246
- Never overwrite `C:\onyxion-ali\.env` wholesale or wipe `C:\onyxion-ali\state\` (single-line edits of .env are OK when Ali asks).
- Never leave a visible console for status checks. Demo account only.
- Do not bypass the safety gate for live apply. Never print passwords in chat/logs; never commit real passwords.

## 2. What the system is

- **Ali PC bridge** (`C:\onyxion-ali`, installed from `Ali PC Deployment/runtime/`): `bridge_trader.py` runs `mt5_live_engine.LatestModsEngine` on closed M15 XAUUSDm bars from the local MT5 (Exness demo 472640728, 1:200) and sends market orders; `watchdog_exness.py` restarts it; `connection_monitor.py` checks MT5/desk every 5 min; `github_update_agent.py` polls GitHub every 90 s and applies pushes candle-safe via `scripts/auto_update_bridge.ps1` (never touches .env/state, never flattens). Four hidden scheduled tasks `OnyxionAli-*` (register_tasks.ps1). `START_BOT.bat` brings everything up and prints EVERYTHING IS NOW LIVE AND RUNNING.
- **GCP desk** (`Google Console Deployment/app/`, FastAPI): live page `/live`, Lab/Testing tab `/` with a JavaScript port of the engine for backtests. It is redeployed only by Hamza's office watcher; we can only read it over HTTPS. `mt5_live_engine.py` and `bridge_trader.py` must stay byte-identical in both folders (safety gate checks).
- **Config precedence**: `.env` on the Ali PC governs; desk Live Controls apply only once someone presses Save on the desk (`updated_at` set). Desk resets/redeploys never change Ali's config.
- **Rules (TRADING_RULES.md)**: raw MT5 bar → Heikin-Ashi; EVERY rule runs on the HA candle (body-break gate, X-Trend clearance via KJ GagaTrend on HA, SUPP wick cross, fill level, stop test, trailing, ATR). Histogram = EMA(5) of Wilder RSI(3) on the REAL close (matches the MQ5 indicator). Colour at HIST_THRESH, trade only when |hist| ≥ HIST_ENTRY_THRESH. Stop = TSL_ATR_MULT × ATR(14) per ticket, trails to each HA close; amber flattens; margin < 50 % flattens. Live fills are the real ask/bid.
- Ali PC `.env` (as of 2026-09-22): ENTRY_EVERY_CANDLE=1, HIST_THRESH=15, HIST_ENTRY_THRESH unset (=15), MAX_SUPP=0, MAXPOS=20, VOLUME=0.02, TSL_ATR_MULT=1.25, XTREND_SOURCE=gaga, LEVERAGE=200, TRAIL_LIVE unset (=1 since Hamza's 564bd09).

## 3. How to verify things (commands that worked)

```bash
# repo vs GitHub vs installed
cd "/c/Users/verye/OneDrive/Documents/Asim - Hamza Bot Code Files" && git fetch -q origin && git status -sb | head -1
diff -q --strip-trailing-cr "Ali PC Deployment/runtime/bridge_trader.py" /c/onyxion-ali/bridge_trader.py
# tests + gate (venv python has MetaTrader5 + fastapi)
cd "Ali PC Deployment/runtime" && /c/onyxion-ali/venv/Scripts/python.exe -m unittest discover -s . -p "test_*.py"
cd "Google Console Deployment/app" && /c/onyxion-ali/venv/Scripts/python.exe -m unittest discover -s . -p "test_*.py"
/c/onyxion-ali/venv/Scripts/python.exe "Ali PC Deployment/runtime/safety_gate_check.py" --root .
# live bot
/c/onyxion-ali/venv/Scripts/python.exe /c/onyxion-ali/scripts/check_bridge_live.py
cat /c/onyxion-ali/state/bridge_status.json ; tail /c/onyxion-ali/logs/github_update_agent.log ; tail /c/onyxion-ali/logs/bridge_YYYYMMDD.log
curl -sk https://35.253.21.246/api/broker/state   # public; /api/broker/controls shows desk-saved controls + updated_at
curl -sk https://35.253.21.246/health             # 'decision_candle: heikin_ashi' appears once the desk is redeployed
```
Local desk for browser checks: run `run_desk.py` from the session scratchpad (throwaway login, scratch data dir, port 8090), mint a session token with `auth.create_session_token`, set the `asim_session` cookie in the Browser pane.

Backtest harness (validated, matches desk JS to the cent): `bt_thresh2.py CSV FROM TO rawclose --spread 0.26 [--th 12,15] [--adds N] [--histcap X] [--atr 2.0] [--monthly]` in the 2026-09-20/22 session scratchpad; `fetch_mt5_bars.py N out.csv` exports M15 bars read-only from the running MT5. Pitfall: set `XTREND_SOURCE=gaga` BEFORE computing X-Trend or you get SuperTrend.

## 4. Decision log

- **2026-09-13** Full cleanup (commit 92032e5). Removed AWS/Lambda/zip legacy; fixed: hist colour hard-coded at 10, desk controls overwriting .env every 2 s, updater restart that never worked, 401 spam, adds sent with the primary's stop, partial close by nearest entry. Ali chose: remove all legacy, ".env wins unless saved on desk", port live rules into the desk backtester. Engine/desk parity verified.
- **2026-09-13** START_BOT.bat rewritten (8a0f905): always ensures monitor + GitHub agent, one verdict line.
- **2026-09-14..17 (Hamza)** 15 commits: rolled repo defaults back to classic mode / HIST_THRESH 10 / MAX_SUPP 10, added HIST_ENTRY_THRESH=15, decision-candle arrows, desk session wipe. Ali's .env kept every-candle/15.
- **2026-09-20** Ali: "only Heikin-Ashi candles for display and for every decision; SUPP when the next candle's body or wick crosses the last trade candle's wick" → commit 46fbbce (engine, bridge, both dashboards, tests, TRADING_RULES). Ali: leverage is 1:200 → 7a2ea73 everywhere. Full review (eb8bde7): fixed retry index corruption, ghost tickets for unsent entries, positions_get None dropping tickets, SL_MODIFY only for the primary, state save race, stale-entry window 1800→180 s, updater restarting the bridge outside the candle-safe window on every push, agent stuck after a blocked update, desk MAX_SUPP 0→10 bug, missing entry-threshold gate in the JS tester.
- **2026-09-20 (Hamza)** f1c5d52 desk shows leverage + last apply; **2026-09-22 (Hamza)** 564bd09 TRAIL_LIVE=1 (stops tighten on live ticks between closes, on by default), f6b9948 colour back to ±10, desk P&L tables.
- **2026-09-22** Trade analysis + threshold study. Found and corrected: all earlier backtests filled at the HA close/body (optimistic); only real-close fills + 0.26 spread reproduce the live account. Desk "Market close (live-like)" mode fixed (05508f0). Results with real fills, TH 15, current config: Sep 14-22 −957, Jun 23–Sep 22 −1,975 (blown), Apr–Aug blown, Feb blown. With adds ≤ 3 + no entry when |hist| > 30 + ATR 2.0: Sep +393, Jun–Sep +1,354 (all months +), Apr–Aug +1,919 (all months +), Feb −714. TH 12 is not better. Sep 20→22 only: current −202 vs adds≤3+hist≤30 −34 (ATR 2.0 hurts in that chop). **Recommendation given, not applied**: MAXPOS=4, TSL_ATR_MULT=2.0 in .env, new HIST_ENTRY_MAX=30 control; then test a daily loss stop. Ali has not said yes yet.
- **2026-09-22 17:26 UTC** Hamza pressed Save on the desk Live Controls (`hamza-hist10`): ENTRY_EVERY_CANDLE=0, HIST_THRESH=10, MAX_SUPP=10, BEST_LOT_MULT=2.0. The bridge applied them (desk wins once saved), so Ali's bot has been in classic mode since then even though his .env says every-candle/15. Reported to Ali; not reverted — his call.

## 5. Current state (refresh each session)

- GitHub main = local = installed on Ali PC: `3ed9360` (2026-09-22 19:00 UTC). Tests 48 runtime + 11 desk pass, gate PASS.
- GCP desk still on a build older than 17 Sep as of 2026-09-22 (no `decision_candle` on /health); Hamza's watcher has not redeployed. Its account panel shows 1:200 from the bridge heartbeat.
- Live: bridge ONLINE, mode reported `classic_amber_supp__atr1.25`, `live_controls_source: desk` (see 17:26 event). Account reset to 2,000 on Sep 20; 1,937 balance on Sep 22 18:45 UTC.
- Open questions for Ali: apply the loss controls? keep desk-saved classic controls or make .env win again?

## 6. Known open risks (deliberately not fixed)

Abandoned close legs after 8 failed attempts can orphan broker positions; entry TIMEOUT retcode retried without checking for a fresh fill; SKIP_WEEKENDS uses a UTC-4 clock (OFF live); deploy-skip marker targets the bar after the restart bar; desk JS spread field is points × lot (engine: flat USD); JS lacks SKIP_WEEKENDS and hard-codes best-hours lot ×2 (both OFF live).

## 7. Session log (append)

- 2026-09-13 session f8b66f4b: cleanup, START_BOT, memory files created.
- 2026-09-20 session 3e436f2f: fetch/apply check, HA rules, leverage 1:200, full review, GCP watch (no redeploy).
- 2026-09-22 (same session): trade autopsy, threshold study, fill-model correction, TRAIL_LIVE noticed, desk-control override noticed, CONTEXT.md created.
