//+------------------------------------------------------------------+
//|                                          OnyxionXTrendProxy.mq5  |
//|  Visual X-Trend = KJ GagaTrend on Heiken-Ashi (Vintage match).   |
//|  Replaces SuperTrend(6,0.8) proxy. Overlay on main chart.        |
//+------------------------------------------------------------------+
#property copyright "Onyxion"
#property version   "2.00"
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   1

#property indicator_label1  "X-Trend Gaga"
#property indicator_type1   DRAW_COLOR_LINE
#property indicator_color1  clrDodgerBlue, clrOrangeRed
#property indicator_style1  STYLE_SOLID
#property indicator_width1  2

input int XT_Window = 160;  // closed-bar window (match bridge)

double LineBuf[];
double ColorBuf[];

int OnInit()
{
   SetIndexBuffer(0, LineBuf,  INDICATOR_DATA);
   SetIndexBuffer(1, ColorBuf, INDICATOR_COLOR_INDEX);
   ArraySetAsSeries(LineBuf,  true);
   ArraySetAsSeries(ColorBuf, true);
   IndicatorSetString(INDICATOR_SHORTNAME, "X-Trend Gaga (HA)");
   return(INIT_SUCCEEDED);
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
   if(rates_total < 20)
      return(0);

   ArraySetAsSeries(open,  true);
   ArraySetAsSeries(high,  true);
   ArraySetAsSeries(low,   true);
   ArraySetAsSeries(close, true);

   int n = MathMin(rates_total, MathMax(30, XT_Window + 1));
   double hao[], hac[], hah[], hal[], atr[], xt[], dir[];
   ArrayResize(hao, n);
   ArrayResize(hac, n);
   ArrayResize(hah, n);
   ArrayResize(hal, n);
   ArrayResize(atr, n);
   ArrayResize(xt,  n);
   ArrayResize(dir, n);

   for(int c = 0; c < n; c++)
   {
      int i = n - 1 - c;
      hac[c] = (open[i] + high[i] + low[i] + close[i]) / 4.0;
      hao[c] = (c == 0) ? (open[i] + close[i]) / 2.0
                        : (hao[c - 1] + hac[c - 1]) / 2.0;
      hah[c] = MathMax(high[i], MathMax(hao[c], hac[c]));
      hal[c] = MathMin(low[i],  MathMin(hao[c], hac[c]));
   }

   // Wilder ATR(14) on HA
   int atrLen = 14;
   double alpha = 1.0 / atrLen;
   double sumtr = 0.0;
   for(int c = 0; c < n; c++)
   {
      double tr = (c == 0) ? (hah[0] - hal[0])
                           : MathMax(hah[c] - hal[c],
                             MathMax(MathAbs(hah[c] - hac[c - 1]),
                                     MathAbs(hal[c] - hac[c - 1])));
      if(c < atrLen)
      {
         sumtr += tr;
         atr[c] = (c == atrLen - 1) ? sumtr / atrLen : tr;
      }
      else
         atr[c] = atr[c - 1] * (1.0 - alpha) + tr * alpha;
   }

   // KJ GagaTrend on HA high/low/close
   int trend = 0;
   int nextTrend = 0;
   double maxLowPrice = hal[0];
   double minHighPrice = hah[0];
   double up = EMPTY_VALUE;
   double down = EMPTY_VALUE;
   int prevTrend = trend;

   for(int c = 0; c < n; c++)
   {
      // highestbars(high, 2) / lowestbars(low, 3)
      int highOff = 0;
      double bestH = hah[c];
      for(int off = 1; off < 2 && c - off >= 0; off++)
      {
         if(hah[c - off] > bestH) { bestH = hah[c - off]; highOff = off; }
      }
      int lowOff = 0;
      double bestL = hal[c];
      for(int off = 1; off < 3 && c - off >= 0; off++)
      {
         if(hal[c - off] < bestL) { bestL = hal[c - off]; lowOff = off; }
      }
      double highPrice = hah[c - highOff];
      double lowPrice  = hal[c - lowOff];

      double highMA = (c >= 1) ? (hah[c] + hah[c - 1]) / 2.0 : EMPTY_VALUE;
      double lowFast = (c >= 1) ? (hal[c] + hal[c - 1]) / 2.0 : EMPTY_VALUE;
      double lowMain = (c >= 2) ? (hal[c] + hal[c - 1] + hal[c - 2]) / 3.0 : EMPTY_VALUE;
      double lowSlow = (c >= 3) ? (hal[c] + hal[c - 1] +hal[c - 2] +hal[c - 3]) / 4.0 : EMPTY_VALUE;
      double prevLow  = (c > 0) ?hal[c - 1] :hal[c];
      double prevHigh = (c > 0) ?hah[c - 1] :hah[c];

      if(nextTrend == 1)
      {
         maxLowPrice = MathMax(lowPrice, maxLowPrice);
         bool bearishConf = (highMA != EMPTY_VALUE && highMA < maxLowPrice);
         bool bearishBreak = (hac[c] < prevLow);
         if(bearishConf && bearishBreak)
         {
            trend = 1;
            nextTrend = 0;
            minHighPrice = highPrice;
         }
      }
      else
      {
         minHighPrice = MathMin(highPrice, minHighPrice);
         bool bullishMain =
            (lowMain != EMPTY_VALUE && lowMain > minHighPrice) ||
            (lowSlow != EMPTY_VALUE && lowSlow > minHighPrice);
         bool bullishFast = (lowFast != EMPTY_VALUE && lowFast > minHighPrice);
         bool mainAlmost =
            (lowMain != EMPTY_VALUE) &&
            (lowMain >= (minHighPrice - atr[c] * 0.025));
         bool bullishConf = bullishMain || (bullishFast && mainAlmost);
         bool bullishBreak = (hac[c] > prevHigh);
         if(bullishConf && bullishBreak)
         {
            trend = 0;
            nextTrend = 1;
            maxLowPrice = lowPrice;
         }
      }

      if(trend == 0)
      {
         if(prevTrend != 0)
            up = (down != EMPTY_VALUE) ? down : maxLowPrice;
         else
         {
            double prevUp = (up != EMPTY_VALUE) ? up : maxLowPrice;
            up = MathMax(maxLowPrice, prevUp);
         }
      }
      else
      {
         if(prevTrend != 1)
            down = (up != EMPTY_VALUE) ? up : minHighPrice;
         else
         {
            double prevDown = (down != EMPTY_VALUE) ? down : minHighPrice;
            down = MathMin(minHighPrice, prevDown);
         }
      }

      xt[c]  = (trend == 0) ? up : down;
      dir[c] = (trend == 0) ? 1 : -1;
      prevTrend = trend;
   }

   for(int i = 0; i < rates_total; i++)
   {
      LineBuf[i] = EMPTY_VALUE;
      ColorBuf[i] = 0;
   }
   for(int c = 0; c < n; c++)
   {
      int i = n - 1 - c;
      LineBuf[i]  = xt[c];
      ColorBuf[i] = (dir[c] == 1) ? 0 : 1;
   }

   return(rates_total);
}
//+------------------------------------------------------------------+
