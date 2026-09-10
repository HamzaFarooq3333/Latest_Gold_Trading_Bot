//+------------------------------------------------------------------+
//|                                          OnyxionHistogram.mq5    |
//|  TradingView-matched QQE histogram (real close, not HA close).     |
//|  hist = EMA( WilderRSI(close, RSI_Period), HistEMA ) − 50         |
//|  green >= +Thresh · amber between · red <= −Thresh               |
//+------------------------------------------------------------------+
#property copyright "Onyxion"
#property version   "1.02"
#property indicator_separate_window
#property indicator_buffers 2
#property indicator_plots   1
#property indicator_minimum -50
#property indicator_maximum  50
#property indicator_level1   10
#property indicator_level2    0
#property indicator_level3  -10
#property indicator_levelcolor clrSilver
#property indicator_levelstyle STYLE_DOT
#property indicator_levelwidth 1

#property indicator_label1  "Hist"
#property indicator_type1   DRAW_COLOR_HISTOGRAM
#property indicator_color1  clrLimeGreen, clrOrange, clrCrimson
#property indicator_style1  STYLE_SOLID
#property indicator_width1  3

input int  RSI_Period     = 3;     // RSI length (match TV)
input int  HistEMA        = 5;     // EMA span of RSI (match TV)
input int  HistThresh     = 10;    // green/red threshold
input bool ShowBarValues  = true;  // signed values on recent bars
input int  ValueBars      = 12;    // how many bars to label

#define SHORTNAME   "Onyxion Hist"
#define OBJ_PREFIX  "OnyxionHistV_"
#define OBJ_NOW     "OnyxionHistNow"

double HistBuf[];
double ColorBuf[];
datetime lastLabelBar = 0;

int ZoneOf(const double h)
{
   if(h >= HistThresh) return 0;   // green
   if(h <= -HistThresh) return 2;  // red
   return 1;                       // amber
}

color ZoneColor(const int z)
{
   if(z == 0) return clrLimeGreen;
   if(z == 2) return clrCrimson;
   return clrOrange;
}

string ZoneName(const int z)
{
   if(z == 0) return "GREEN";
   if(z == 2) return "RED";
   return "AMBER";
}

// Wilder RSI on chronological close (oldest index 0) — matches pandas/TV.
void WilderRsiChrono(const double &close[], const int n, const int period, double &rsi[])
{
   ArrayResize(rsi, n);
   ArrayInitialize(rsi, EMPTY_VALUE);
   if(n < 2 || period < 1)
      return;

   double avgG = 0.0, avgL = 0.0;
   for(int i = 1; i < n; i++)
   {
      double d = close[i] - close[i - 1];
      double g = (d > 0.0) ? d : 0.0;
      double l = (d < 0.0) ? -d : 0.0;

      if(i == 1)
      {
         avgG = g;
         avgL = l;
      }
      else
      {
         avgG = avgG + (g - avgG) / period;
         avgL = avgL + (l - avgL) / period;
      }

      if(avgL <= 0.0)
         rsi[i] = 100.0;
      else
      {
         double rs = avgG / avgL;
         rsi[i] = 100.0 - 100.0 / (1.0 + rs);
      }
   }
   rsi[0] = 50.0;
}

// EMA(span) on chronological series — pandas ewm(span, adjust=False).
void EmaSpanChrono(const double &src[], const int n, const int span, double &out[])
{
   ArrayResize(out, n);
   if(n <= 0)
      return;
   double alpha = 2.0 / (span + 1.0);
   out[0] = src[0];
   for(int i = 1; i < n; i++)
      out[i] = alpha * src[i] + (1.0 - alpha) * out[i - 1];
}

// Build hist buffer from real close[] (MT5 series: 0 = newest).
bool BuildHistFromClose(const double &close[], const int rates_total,
                        const int rsiPeriod, const int emaSpan)
{
   if(rates_total < rsiPeriod + emaSpan + 5)
      return false;

   double chrono[];
   ArrayResize(chrono, rates_total);
   for(int c = 0; c < rates_total; c++)
      chrono[c] = close[rates_total - 1 - c];

   double rsi[], ema[];
   WilderRsiChrono(chrono, rates_total, rsiPeriod, rsi);
   EmaSpanChrono(rsi, rates_total, emaSpan, ema);

   for(int c = 0; c < rates_total; c++)
   {
      int s = rates_total - 1 - c;
      if(rsi[c] == EMPTY_VALUE)
      {
         HistBuf[s] = EMPTY_VALUE;
         ColorBuf[s] = 1;
         continue;
      }
      double h = ema[c] - 50.0;
      HistBuf[s] = h;
      ColorBuf[s] = ZoneOf(h);
   }
   return true;
}

int OnInit()
{
   SetIndexBuffer(0, HistBuf, INDICATOR_DATA);
   SetIndexBuffer(1, ColorBuf, INDICATOR_COLOR_INDEX);
   ArraySetAsSeries(HistBuf, true);
   ArraySetAsSeries(ColorBuf, true);
   PlotIndexSetInteger(0, PLOT_DRAW_TYPE, DRAW_COLOR_HISTOGRAM);
   PlotIndexSetInteger(0, PLOT_SHOW_DATA, true);
   PlotIndexSetDouble(0, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   IndicatorSetInteger(INDICATOR_DIGITS, 2);
   IndicatorSetString(INDICATOR_SHORTNAME, SHORTNAME);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   ObjectsDeleteAll(0, OBJ_PREFIX);
   ObjectDelete(0, OBJ_NOW);
}

void DrawValues(const datetime &time[], const int rates_total)
{
   int win = ChartWindowFind(0, SHORTNAME);
   if(win < 0)
      return;

   double closed = (rates_total > 1 && HistBuf[1] != EMPTY_VALUE) ? HistBuf[1] : HistBuf[0];
   double live   = HistBuf[0];
   int zClosed = ZoneOf(closed);
   string txt = StringFormat("Hist %+.2f %s   live %+.2f", closed, ZoneName(zClosed), live);

   if(ObjectFind(0, OBJ_NOW) < 0)
   {
      ObjectCreate(0, OBJ_NOW, OBJ_LABEL, win, 0, 0);
      ObjectSetInteger(0, OBJ_NOW, OBJPROP_CORNER, CORNER_RIGHT_UPPER);
      ObjectSetInteger(0, OBJ_NOW, OBJPROP_ANCHOR, ANCHOR_RIGHT_UPPER);
      ObjectSetInteger(0, OBJ_NOW, OBJPROP_XDISTANCE, 8);
      ObjectSetInteger(0, OBJ_NOW, OBJPROP_YDISTANCE, 16);
      ObjectSetInteger(0, OBJ_NOW, OBJPROP_FONTSIZE, 10);
      ObjectSetString(0, OBJ_NOW, OBJPROP_FONT, "Arial Bold");
      ObjectSetInteger(0, OBJ_NOW, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, OBJ_NOW, OBJPROP_HIDDEN, true);
   }
   ObjectSetString(0, OBJ_NOW, OBJPROP_TEXT, txt);
   ObjectSetInteger(0, OBJ_NOW, OBJPROP_COLOR, ZoneColor(zClosed));

   if(!ShowBarValues || ValueBars <= 0)
      return;

   datetime t0 = time[0];
   if(t0 == lastLabelBar)
      return;
   lastLabelBar = t0;

   ObjectsDeleteAll(0, OBJ_PREFIX);
   int n = MathMin(ValueBars, rates_total);
   for(int i = 0; i < n; i++)
   {
      if(HistBuf[i] == EMPTY_VALUE)
         continue;
      string name = OBJ_PREFIX + IntegerToString(i) + "_" + IntegerToString((int)time[i]);
      if(!ObjectCreate(0, name, OBJ_TEXT, win, time[i], HistBuf[i]))
         continue;
      ObjectSetString(0, name, OBJPROP_TEXT, StringFormat("%+.1f", HistBuf[i]));
      ObjectSetInteger(0, name, OBJPROP_COLOR, ZoneColor(ZoneOf(HistBuf[i])));
      ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
      ObjectSetString(0, name, OBJPROP_FONT, "Arial Bold");
      ObjectSetInteger(0, name, OBJPROP_ANCHOR,
                       (HistBuf[i] >= 0.0) ? ANCHOR_LOWER : ANCHOR_UPPER);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   }
}

int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{
   if(rates_total < RSI_Period + HistEMA + 5)
      return 0;

   ArraySetAsSeries(close, true);
   ArraySetAsSeries(time, true);

   if(!BuildHistFromClose(close, rates_total, RSI_Period, HistEMA))
      return prev_calculated;

   DrawValues(time, rates_total);
   return rates_total;
}
//+------------------------------------------------------------------+
