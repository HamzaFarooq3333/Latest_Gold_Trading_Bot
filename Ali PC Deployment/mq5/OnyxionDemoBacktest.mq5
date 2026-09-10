//+------------------------------------------------------------------+
//|                                      OnyxionDemoBacktest.mq5     |
//|  LIVE + Strategy Tester — Histogram v2 / Asim rules on MT5 data. |
//|                                                                  |
//|  NO AWS / NO Lab Bridge. All inputs from this chart's candles.   |
//|                                                                  |
//|  hist   = EMA(RSI(close,3), 5) − 50   (raw MT5 close)             |
//|           green > +10 · amber between · red < -10                |
//|  X-Trend = SuperTrend on Heiken-Ashi (Period 6 / Mult 0.8)       |
//|           via OnyxionXTrendProxy (proxy, not Vintage TV XT)      |
//|                                                                  |
//|  Entry  : after amber, histogram colour + previous HA BODY cross |
//|           + HA candle clear of SuperTrend XT                     |
//|  Adds   : same cross + progressive candle close (supp)            |
//|  Trail  : 0.25 model points every closed bar                     |
//|  Exit   : amber flatten · stop hit · weekend / hour filters      |
//|                                                                  |
//|  Attach to Exness XAUUSDm M15. Algo Trading ON.                  |
//+------------------------------------------------------------------+
#property copyright "Onyxion"
#property version   "1.24"
#property strict
#include <Trade/Trade.mqh>

input double LotSize          = 0.01;
input double BestLotMult      = 2.0;
input int    HistThresh       = 10;
input int    RSI_Period       = 3;
input int    HistEMA          = 5;
input double TslPoints        = 0.25;  // Histogram v2 model trail; broker may widen actual SL
input double StopSlippagePts  = 0.25;
input int    MaxPositions     = 20;
input int    MaxSupp          = 10;
input bool   UseXTrendGate    = true;   // SuperTrend XT clearance ON
input int    XT_Period        = 6;      // SuperTrend period
input double XT_Mult          = 0.8;    // SuperTrend multiplier
input int    XT_Window        = 160;    // same MT5 history window as AWS
input double XT_Buf           = 0.0;
input bool   ShowXTrendOnChart = true;  // draw SuperTrend proxy on chart
input bool   ShowHistogramOnChart = true; // colored hist bars + values at bottom
input bool   SkipWeekends     = true;   // Sat/Sun UTC-4
input bool   SkipWorstHours   = true;   // UTC-4 worst hours
input bool   FocusBestHours   = true;   // 2x lot in best hours UTC-4
input int    TzOffsetHours    = -4;     // UTC-4
input ulong  MagicNumber      = 20260826;
input int    SlippagePoints   = 30;

CTrade trade;
int    rsiHandle = INVALID_HANDLE;
int    xtHandle  = INVALID_HANDLE;   // OnyxionXTrendProxy (visual + buffer)
int    histHandle = INVALID_HANDLE;  // OnyxionHistogram (separate pane)
bool   histOnChart = false;
datetime lastBar = 0;

//--- engine state (closed-bar logic) --------------------------------
bool   seenAmber = false;
int    runSide   = 0;   // +1 long run, -1 short, 0 flat run
double prevO = 0, prevH = 0, prevL = 0, prevC = 0;
bool   havePrev = false;
double lastTradeClose = 0;
bool   haveLastTradeClose = false;

struct UnitPos
{
   ulong  ticket;
   double entry;
   double sl;
   bool   primary;
   double lot;
};
UnitPos units[];
int     nUnits = 0;

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(SlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);
   rsiHandle = iRSI(_Symbol, _Period, RSI_Period, PRICE_CLOSE);
   if(rsiHandle == INVALID_HANDLE)
   {
      Print("OnyxionDemoBacktest: RSI handle failed");
      return INIT_FAILED;
   }

   // Same indicator as Navigator → Indicators → X Trend → OnyxionXTrendProxy
   // Path must match the folder under MQL5\Indicators\
   xtHandle = iCustom(_Symbol, _Period, "X Trend\\OnyxionXTrendProxy",
                      XT_Period, XT_Mult, XT_Window);
   if(xtHandle == INVALID_HANDLE)
   {
      Print("WARN: could not load X Trend\\OnyxionXTrendProxy — using internal SuperTrend only. Error=",
            GetLastError());
   }
   else if(ShowXTrendOnChart)
   {
      // Puts the line on the Strategy Tester visual chart AND live charts
      long cid = ChartID();
      if(!ChartIndicatorAdd(cid, 0, xtHandle))
         Print("WARN: ChartIndicatorAdd failed err=", GetLastError(),
               " — drag OnyxionXTrendProxy onto the tester chart manually");
      else
         Print("X-Trend proxy attached to chart (visible in Visual backtest)");
   }

   AttachHistogramPane();

   ArrayResize(units, 0);
   nUnits = 0;
   seenAmber = false;
   runSide = 0;
   havePrev = false;
   Print("OnyxionDemoBacktest v1.24 LIVE/LOCAL ready on ", _Symbol, " ", EnumToString(_Period),
         " | source=MT5 OHLC (no AWS)",
         " | hist=EMA(RSI(", RSI_Period, "),", HistEMA, ")-50",
         " | XT=SuperTrend HA ", XT_Period, "/", DoubleToString(XT_Mult, 2),
         " | gate=", (UseXTrendGate ? "ON" : "OFF"),
         " | trail=", DoubleToString(BrokerMinStopDist(), 2),
         " (TslPoints=", DoubleToString(TslPoints, 2), " or spread+stops)",
         " | xtHandle=", xtHandle);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(rsiHandle != INVALID_HANDLE) IndicatorRelease(rsiHandle);
   // Do not release xtHandle if it was added to the chart — ChartIndicatorAdd owns it.
   // Releasing here can remove the line mid-session; only release if never added.
   if(xtHandle != INVALID_HANDLE && !ShowXTrendOnChart)
      IndicatorRelease(xtHandle);
   if(histHandle != INVALID_HANDLE && !histOnChart)
      IndicatorRelease(histHandle);
}

bool HistogramAlreadyOnChart(const long cid)
{
   int wins = (int)ChartGetInteger(cid, CHART_WINDOWS_TOTAL);
   for(int w = 0; w < wins; w++)
   {
      int n = ChartIndicatorsTotal(cid, w);
      for(int i = 0; i < n; i++)
      {
         string nm = ChartIndicatorName(cid, w, i);
         if(StringFind(nm, "Onyxion Hist") >= 0)
            return true;
      }
   }
   return false;
}

void AttachHistogramPane()
{
   if(!ShowHistogramOnChart)
      return;

   ResetLastError();
   histHandle = iCustom(_Symbol, _Period, "X Trend\\OnyxionHistogram",
                        RSI_Period, HistEMA, HistThresh, true, 24);
   if(histHandle == INVALID_HANDLE)
   {
      ResetLastError();
      histHandle = iCustom(_Symbol, _Period, "OnyxionHistogram",
                           RSI_Period, HistEMA, HistThresh, true, 24);
   }
   if(histHandle == INVALID_HANDLE)
   {
      Print("WARN: OnyxionHistogram not found err=", GetLastError(),
            " — compile Indicators\\X Trend\\OnyxionHistogram.mq5 then reattach");
      return;
   }

   long cid = ChartID();
   if(HistogramAlreadyOnChart(cid))
   {
      histOnChart = true;
      Print("Histogram pane already on chart");
      return;
   }

   int sub = (int)ChartGetInteger(cid, CHART_WINDOWS_TOTAL);
   ResetLastError();
   if(ChartIndicatorAdd(cid, sub, histHandle))
   {
      histOnChart = true;
      Print("Histogram pane attached (colored bars + values, subwindow ", sub, ")");
   }
   else
      Print("WARN: ChartIndicatorAdd histogram err=", GetLastError(),
            " — drag OnyxionHistogram from Navigator (Indicators → X Trend) onto the chart");
}

//+------------------------------------------------------------------+
bool IsNewBar()
{
   datetime t = iTime(_Symbol, _Period, 0);
   if(t == 0 || t == lastBar) return false;
   lastBar = t;
   return true;
}

int Zone(double h)
{
   if(h > HistThresh) return 1;
   if(h < -HistThresh) return -1;
   return 0;
}

double BodyHigh(double o, double c) { return (o >= c) ? o : c; }
double BodyLow (double o, double c) { return (o >= c) ? c : o; }

// Local hour in TzOffsetHours from bar time (server/broker time treated as UTC)
int HourLocal(datetime t)
{
   MqlDateTime dt;
   TimeToStruct(t + TzOffsetHours * 3600, dt);
   return dt.hour;
}

bool IsWeekendLocal(datetime t)
{
   MqlDateTime dt;
   TimeToStruct(t + TzOffsetHours * 3600, dt);
   return (dt.day_of_week == 0 || dt.day_of_week == 6); // Sun=0 Sat=6 in MqlDateTime
}

bool HourAllowed(int hour)
{
   if(!SkipWorstHours) return true;
   // WORST_HOURS_UTC4 = {0,3,5,17,18}
   return !(hour == 0 || hour == 3 || hour == 5 || hour == 17 || hour == 18);
}

double LotForHour(int hour)
{
   if(FocusBestHours && (hour == 2 || hour == 9 || hour == 10 ||
                         hour == 11 || hour == 19 || hour == 21))
      return LotSize * BestLotMult;
   return LotSize;
}

//--- hist on last CLOSED bar (shift 1) — TV formula on real close ----------
bool HistFromCloseSeries(const double &close[], const int n, const int shift, double &hist)
{
   if(n < HistEMA + RSI_Period + 5 || shift < 0 || shift >= n)
      return false;

   double chrono[];
   ArrayResize(chrono, n);
   for(int c = 0; c < n; c++)
      chrono[c] = close[n - 1 - c];

   double rsi[], ema[];
   ArrayResize(rsi, n);
   ArrayInitialize(rsi, EMPTY_VALUE);
   double avgG = 0.0, avgL = 0.0;
   for(int i = 1; i < n; i++)
   {
      double d = chrono[i] - chrono[i - 1];
      double g = (d > 0.0) ? d : 0.0;
      double l = (d < 0.0) ? -d : 0.0;
      if(i == 1) { avgG = g; avgL = l; }
      else { avgG = avgG + (g - avgG) / RSI_Period; avgL = avgL + (l - avgL) / RSI_Period; }
      rsi[i] = (avgL <= 0.0) ? 100.0 : 100.0 - 100.0 / (1.0 + avgG / avgL);
   }
   rsi[0] = 50.0;

   ArrayResize(ema, n);
   double alpha = 2.0 / (HistEMA + 1.0);
   ema[0] = rsi[0];
   for(int i = 1; i < n; i++)
      ema[i] = alpha * rsi[i] + (1.0 - alpha) * ema[i - 1];

   int cShift = n - 1 - shift;
   hist = ema[cShift] - 50.0;
   return true;
}

bool GetHist(double &hist)
{
   // Match AWS: 160 closed MT5 bars plus the current forming bar.
   int need = MathMax(XT_Window + 1, HistEMA * 10 + RSI_Period * 10 + 20);
   double close[];
   ArraySetAsSeries(close, true);
   if(CopyClose(_Symbol, _Period, 0, need, close) < need)
      return false;
   return HistFromCloseSeries(close, ArraySize(close), 1, hist);
}

//--- HA signal candle for the requested MT5 shift ---------------------------
// Histogram v2 gates entries with HA OHLC, while execution and stop fills use
// the raw MT5 OHLC from the same bar.
bool GetHaBar(const int shift, double &o, double &h, double &l, double &c)
{
   // Match the AWS HA seed and keep the requested MT5 bar in the window.
   int want = MathMax(XT_Window + 1, shift + 1);
   MqlRates r[];
   ArraySetAsSeries(r, false);
   int got = CopyRates(_Symbol, _Period, 0, want, r);
   if(got < XT_Period + 20 || shift < 0 || shift >= got)
      return false;

   double hao[], hac[], hah[], hal[];
   ArrayResize(hao, got);
   ArrayResize(hac, got);
   ArrayResize(hah, got);
   ArrayResize(hal, got);
   for(int i = 0; i < got; i++)
   {
      hac[i] = (r[i].open + r[i].high + r[i].low + r[i].close) / 4.0;
      hao[i] = (i == 0) ? (r[i].open + r[i].close) / 2.0
                        : (hao[i - 1] + hac[i - 1]) / 2.0;
      hah[i] = MathMax(r[i].high, MathMax(hao[i], hac[i]));
      hal[i] = MathMin(r[i].low, MathMin(hao[i], hac[i]));
   }

   int idx = got - 1 - shift;
   o = hao[idx];
   h = hah[idx];
   l = hal[idx];
   c = hac[idx];
   return true;
}

//--- SuperTrend proxy: exact closed-bar MT5 calculation used by AWS ----------
bool GetXTrendProxy(double &xt)
{
   // Do not read the visual indicator buffer here: its full chart history can
   // seed SuperTrend differently from the AWS MT5-bar calculation. Decisions
   // always use the same raw MT5 M15 window and closed bar as AWS.
   int want = MathMax(30, XT_Window);
   MqlRates r[];
   ArraySetAsSeries(r, false);
   int got = CopyRates(_Symbol, _Period, 0, want + 1, r);
   if(got < XT_Period + 30) return false;
   int n = got - 1; // drop forming bar

   double hao[], hac[], hah[], hal[], atr[];
   ArrayResize(hao, n); ArrayResize(hac, n);
   ArrayResize(hah, n); ArrayResize(hal, n); ArrayResize(atr, n);

   for(int i = 0; i < n; i++)
   {
      hac[i] = (r[i].open + r[i].high + r[i].low + r[i].close) / 4.0;
      hao[i] = (i == 0) ? (r[i].open + r[i].close) / 2.0
                        : (hao[i - 1] + hac[i - 1]) / 2.0;
      hah[i] = MathMax(r[i].high, MathMax(hao[i], hac[i]));
      hal[i] = MathMin(r[i].low,  MathMin(hao[i], hac[i]));
   }

   double alpha = 1.0 / XT_Period, sumtr = 0.0;
   for(int i = 0; i < n; i++)
   {
      double tr = (i == 0) ? (hah[0] - hal[0])
                           : MathMax(hah[i] - hal[i],
                             MathMax(MathAbs(hah[i] - hac[i - 1]),
                                     MathAbs(hal[i] - hac[i - 1])));
      if(i < XT_Period)
      {
         sumtr += tr;
         atr[i] = (i == XT_Period - 1) ? sumtr / XT_Period : tr;
      }
      else atr[i] = atr[i - 1] * (1.0 - alpha) + tr * alpha;
   }

   double fu = 0, fl = 0, st = 0;
   int dir = 1;
   for(int i = 0; i < n; i++)
   {
      double u  = hac[i] - XT_Mult * atr[i];
      double dd = hac[i] + XT_Mult * atr[i];
      if(i == 0) { fu = u; fl = dd; dir = 1; st = u; continue; }
      fu = (hac[i - 1] > fu) ? MathMax(u, fu) : u;
      fl = (hac[i - 1] < fl) ? MathMin(dd, fl) : dd;
      if(dir == 1 && hac[i] < fu) dir = -1;
      else if(dir == -1 && hac[i] > fl) dir = 1;
      st = (dir == 1) ? fu : fl;
   }
   xt = st;
   return true;
}

bool XtClear(double h, double l, double xt, int want)
{
   if(!UseXTrendGate) return true;
   if(want > 0) return (l > xt + XT_Buf);
   return (h < xt - XT_Buf);
}

bool BrokePrevBody(double h, double l, int want)
{
   if(!havePrev) return false;
   if(want > 0) return (h > BodyHigh(prevO, prevC));
   return (l < BodyLow(prevO, prevC));
}

bool EntryOk(double h, double l, double xt, int want, bool primary, double tradeClose)
{
   if(!BrokePrevBody(h, l, want)) return false;
   if(!XtClear(h, l, xt, want)) return false;
   if(!primary && haveLastTradeClose)
   {
      if(want > 0 && tradeClose <= lastTradeClose) return false;
      if(want < 0 && tradeClose >= lastTradeClose) return false;
   }
   return true;
}

double CrossFill(int want, double eo, double eh, double el)
{
   if(want > 0)
   {
      double lvl = BodyHigh(prevO, prevC);
      if(eo > lvl) return eo;
      return lvl;
   }
   double lvl = BodyLow(prevO, prevC);
   if(eo < lvl) return eo;
   return lvl;
}

double BrokerMinStopDist()
{
   // Vantage XAUUSD: stops_level is in points; SL is checked vs Bid/Ask, not fill.
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point <= 0.0) point = 0.01;
   long stops = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   long freeze = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double spread = (ask > bid && bid > 0.0) ? (ask - bid) : 0.0;
   double dist = spread + (double)(stops + 2) * point + (double)freeze * point;
   if(dist < TslPoints) dist = TslPoints;
   return dist;
}

double ClampBrokerSl(double sl, int want)
{
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point <= 0.0) point = 0.01;
   long stops = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double md = (double)(stops + 2) * point;
   if(want > 0)
   {
      double mx = bid - md;
      if(sl > mx) sl = mx;
   }
   else
   {
      double mn = ask + md;
      if(sl < mn) sl = mn;
   }
   return NormalizeDouble(sl, digits);
}

double InitSl(double entry, int want)
{
   double dist = BrokerMinStopDist();
   double sl = (want > 0) ? (entry - dist) : (entry + dist);
   return ClampBrokerSl(sl, want);
}

double TrailSl(double sl, double closePx, int side)
{
   double dist = BrokerMinStopDist();
   double nsl = (side > 0) ? MathMax(sl, closePx - dist) : MathMin(sl, closePx + dist);
   return ClampBrokerSl(nsl, side);
}

int CountSupp()
{
   int c = 0;
   for(int i = 0; i < nUnits; i++) if(!units[i].primary) c++;
   return c;
}

void SyncFromBroker()
{
   // Drop tickets that no longer exist (stopped out externally)
   UnitPos keep[];
   int nk = 0;
   for(int i = 0; i < nUnits; i++)
   {
      if(PositionSelectByTicket(units[i].ticket))
      {
         ArrayResize(keep, nk + 1);
         keep[nk++] = units[i];
      }
   }
   ArrayResize(units, nk);
   for(int i = 0; i < nk; i++) units[i] = keep[i];
   nUnits = nk;
   if(nUnits == 0 && runSide != 0)
   {
      // stops cleared all — keep run_side for possible re-entry same colour
   }
}

bool OpenUnit(int want, bool primary, double lot, double fillHint, double o, double h, double l, double c)
{
   if(nUnits + 1 > MaxPositions) return false;
   if(!primary && CountSupp() >= MaxSupp) return false;

   double fill = CrossFill(want, o, h, l);
   // In tester, market order at bar prices — use fill hint when possible
   double price = (want > 0) ? SymbolInfoDouble(_Symbol, SYMBOL_ASK)
                             : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(MQLInfoInteger(MQL_TESTER) || MQLInfoInteger(MQL_OPTIMIZATION))
      price = fill; // approximate mid-bar fill at body cross

   string cmt = primary ? "DEMO_PRIMARY" : "DEMO_SUPP";
   double sl0 = InitSl(price, want);
   bool ok = false;
   if(want > 0) ok = trade.Buy(lot, _Symbol, price, sl0, 0, cmt);
   else         ok = trade.Sell(lot, _Symbol, price, sl0, 0, cmt);
   if(!ok)
   {
      Print("Open failed: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
      return false;
   }

   ulong ticket = trade.ResultOrder();
   // Prefer deal/position ticket
   if(trade.ResultDeal() > 0)
   {
      if(HistoryDealSelect(trade.ResultDeal()))
         ticket = (ulong)HistoryDealGetInteger(trade.ResultDeal(), DEAL_POSITION_ID);
   }
   // Fallback: find newest magic position
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(!PositionSelectByTicket(t)) continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)MagicNumber) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      ticket = t;
      price  = PositionGetDouble(POSITION_PRICE_OPEN);
      break;
   }

   double sl = InitSl(price, want);
   if(!trade.PositionModify(ticket, sl, 0))
      Print("SL modify failed: ", trade.ResultRetcode(), " ",
            trade.ResultRetcodeDescription(), " sl=", DoubleToString(sl, 2));

   ArrayResize(units, nUnits + 1);
   units[nUnits].ticket  = ticket;
   units[nUnits].entry   = price;
   units[nUnits].sl      = sl;
   units[nUnits].primary = primary;
   units[nUnits].lot     = lot;
   nUnits++;
   lastTradeClose = c;
   haveLastTradeClose = true;

   if(primary)
   {
      runSide = want;
      seenAmber = false;
   }
   Print((primary ? "PRIMARY " : "SUPP "), (want > 0 ? "BUY" : "SELL"),
         " @ ", DoubleToString(price, 2), " sl=", DoubleToString(sl, 2),
         " hist zone open");
   return true;
}

void FlattenAll(string reason)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(!PositionSelectByTicket(t)) continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)MagicNumber) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      trade.PositionClose(t);
   }
   ArrayResize(units, 0);
   nUnits = 0;
   haveLastTradeClose = false;
   lastTradeClose = 0;
   Print("FLATTEN ", reason);
}

void ScanStops(double eh, double el, double eo)
{
   for(int i = nUnits - 1; i >= 0; i--)
   {
      if(!PositionSelectByTicket(units[i].ticket))
      {
         // already gone
         for(int j = i; j < nUnits - 1; j++) units[j] = units[j + 1];
         nUnits--;
         ArrayResize(units, nUnits);
         continue;
      }
      int side = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : -1;
      bool hit = (side > 0 && el <= units[i].sl) || (side < 0 && eh >= units[i].sl);
      if(!hit) continue;

      double slipped = (side > 0) ? (units[i].sl - StopSlippagePts)
                                  : (units[i].sl + StopSlippagePts);
      trade.PositionClose(units[i].ticket);
      Print("STOP hit ticket=", units[i].ticket, " slipped~", DoubleToString(slipped, 2));
      for(int j = i; j < nUnits - 1; j++) units[j] = units[j + 1];
      nUnits--;
      ArrayResize(units, nUnits);
   }
}

void TrailAll(double closePx)
{
   for(int i = 0; i < nUnits; i++)
   {
      if(!PositionSelectByTicket(units[i].ticket)) continue;
      int side = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : -1;
      double nsl = TrailSl(units[i].sl, closePx, side);
      if(MathAbs(nsl - units[i].sl) > 1e-9)
      {
         units[i].sl = nsl;
         trade.PositionModify(units[i].ticket, nsl, 0);
      }
   }
}

//+------------------------------------------------------------------+
void OnTick()
{
   // Process once per new bar, using the bar that just closed (shift 1)
   if(!IsNewBar()) return;
   if(Bars(_Symbol, _Period) < 100) return;

   // Raw OHLC is used for execution, stops, and fills.
   double o = iOpen(_Symbol, _Period, 1);
   double h = iHigh(_Symbol, _Period, 1);
   double l = iLow(_Symbol, _Period, 1);
   double c = iClose(_Symbol, _Period, 1);
   datetime t = iTime(_Symbol, _Period, 1);

   // Histogram v2 uses the HA candle as the signal/gate series.
   double so = 0, sh = 0, sl = 0, sc = 0;
   if(!GetHaBar(1, so, sh, sl, sc))
   {
      Print("HA signal compute failed on bar ", TimeToString(t));
      return;
   }

   SyncFromBroker();

   if(SkipWeekends && IsWeekendLocal(t))
   {
      // Do not advance prev body on weekend
      return;
   }

   double hist = 0;
   if(!GetHist(hist))
   {
      Print("hist compute failed on bar ", TimeToString(t));
      return;
   }
   int zz = Zone(hist);

   double xt = 0;
   if(!GetXTrendProxy(xt))
   {
      Print("SuperTrend XT compute failed on bar ", TimeToString(t));
      return;
   }

   int hour = HourLocal(t);
   double lot = LotForHour(hour);
   // Closed-bar heartbeat (once per M15) so Experts shows local calc is alive
   Print("BAR ", TimeToString(t),
         " hist=", DoubleToString(hist, 2),
         " zone=", zz,
         " xt=", DoubleToString(xt, 2),
         " run=", runSide,
         " units=", nUnits,
         " amber=", (seenAmber ? 1 : 0));

   // --- stops on this bar extremes (execution ≈ signal OHLC) ---
   if(nUnits > 0)
      ScanStops(h, l, o);

   // --- amber: flatten + arm ---
   if(zz == 0)
   {
      seenAmber = true;
      if(nUnits > 0)
      {
         FlattenAll("amber");
         runSide = 0;
      }
      else
         runSide = 0;
   }

   // --- trail remaining ---
   if(nUnits > 0 && zz != 0)
      TrailAll(c);

   // --- scale-in while in position ---
   if(nUnits > 0 && zz == (runSide > 0 ? 1 : -1) && runSide != 0)
   {
      if(EntryOk(sh, sl, xt, runSide, false, c) && HourAllowed(hour))
         OpenUnit(runSide, false, lot, o, o, h, l, c);
   }

   // --- re-enter supp path if flat but run still coloured (after stops) ---
   if(nUnits == 0 && runSide != 0 && zz == (runSide > 0 ? 1 : -1))
   {
      if(EntryOk(sh, sl, xt, runSide, false, c) && HourAllowed(hour))
         OpenUnit(runSide, false, lot, o, o, h, l, c);
   }

   // --- primary entry ---
   if(nUnits == 0 && runSide == 0 && seenAmber && havePrev)
   {
      int want = 0;
      if(zz == 1 && EntryOk(sh, sl, xt, 1, true, c)) want = 1;
      else if(zz == -1 && EntryOk(sh, sl, xt, -1, true, c)) want = -1;
      if(want != 0 && HourAllowed(hour))
         OpenUnit(want, true, lot, o, o, h, l, c);
   }

   // advance previous body reference
   // The next body-cross reference must be the previous HA body.
   prevO = so; prevH = sh; prevL = sl; prevC = sc;
   havePrev = true;
}
//+------------------------------------------------------------------+
