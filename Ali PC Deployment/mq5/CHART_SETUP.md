# MT5 chart setup — Histogram + KJ X-Trend (Gaga)

Attach these on **XAUUSDm · M15** only. That is the live period the bridge and GCP desk use.

## Files (already in this pack)

| Source in pack | Install location | Role |
|----------------|------------------|------|
| `mq5\OnyxionHistogram.mq5` | `MQL5\Indicators\` | QQE histogram (separate window) |
| `mq5\OnyxionXTrendProxy.mq5` | `MQL5\Indicators\` | **KJ GagaTrend on HA** (main chart overlay) |
| `mq5\OnyxionMT5ValuePublisher.mq5` | `MQL5\Experts\` | Optional values publisher EA |
| `mq5\OnyxionLabBridge.mq5` | `MQL5\Experts\` | Lab bridge EA (optional) |
| `mq5\OnyxionDemoBacktest.mq5` | `MQL5\Experts\` | Demo helper (optional) |

Installer copies them automatically:

```powershell
powershell -ExecutionPolicy Bypass -File C:\onyxion-ali\scripts\install_mq5_to_terminal.ps1 -Profile ali -Root C:\onyxion-ali
```

## Compile

1. Open MetaEditor from the portable terminal (`metaeditor64.exe` next to `terminal64.exe`).
2. Open each `.mq5` under `MQL5\Indicators` and `MQL5\Experts`.
3. Press **F7** — must be **0 errors**.
4. Confirm `.ex5` appears next to each file.

## Attach on chart (order matters)

1. Open **XAUUSDm**, timeframe **M15**.
2. Navigator → Indicators → Custom:
   - Drag **OnyxionHistogram** onto the chart → Accept defaults (RSI 3 / EMA 5 / Thresh 10). It opens in a **separate** window under the price.
   - Drag **OnyxionXTrendProxy** onto the **main** price chart → Accept. Short name: `X-Trend Gaga (HA)`. Blue/red line on price.
3. (Optional) Navigator → Expert Advisors → attach **OnyxionMT5ValuePublisher** to the same chart. Allow Algo Trading if prompted.
4. Toolbar: **Algo Trading** = green / ON.
5. Chart properties → Common: check **Show trade levels** if you want SL/TP lines visible.

## Clock / period check

- Exness demo chart time = **UTC** (same as broker bar open).
- Live desk chart axis is also **UTC** — bar labels must match MT5 M15 opens (e.g. `:00`, `:15`, `:30`, `:45`).
- If MT5 and `/live` disagree by hours, the browser was showing local time — refresh after the latest desk deploy; bridge sends `time` / `time_unix` / `time_tz=UTC` every heartbeat.

## Do not

- Do not attach SuperTrend for Ali (`XTREND_SOURCE=gaga`).
- Do not use H1/H4 for live — bridge polls **M15**.
- Do not run a second copy of the same EA on another chart for the same account.
