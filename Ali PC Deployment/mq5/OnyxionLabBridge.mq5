//+------------------------------------------------------------------+
//| OnyxionLabBridge.mq5                                              |
//| Poll AWS labs and trade XAUUSD on Vantage demo.                   |
//| Attach to XAUUSD M15. Set Model=DEMO, ASIM, HARD, or SOFT.       |
//+------------------------------------------------------------------+
#property copyright "Onyxion"
#property version   "1.11"
#property strict

input string InpModel        = "DEMO";   // DEMO, ASIM, HARD, or SOFT
input string InpDemoUrl      = "https://azmbjjosjkia5h37iud55ai7nm0ftjzh.lambda-url.us-east-1.on.aws";
input string InpHardUrl      = "https://rpxjjut24apu3su5xzgb2txady0tvwmv.lambda-url.us-east-1.on.aws";
input string InpSoftUrl      = "https://6kjx4gusqba3pvbk3yttk24qoa0xawbv.lambda-url.us-east-1.on.aws";
input string InpAsimUrl      = "https://w5ye4ffzsutba53cyfqyngi4ku0auwzw.lambda-url.us-east-1.on.aws";
input double InpLot          = 0.01;
input int    InpMagicDemo    = 130001;
input int    InpMagicHard    = 110045;
input int    InpMagicSoft    = 115696;
input int    InpMagicAsim    = 126823;
input int    InpMaxUnits     = 5;
input int    InpDeviation    = 30;
input int    InpPollSeconds  = 15;
input string InpSymbol       = "XAUUSD";  // plain gold USD (not XAUUSD+)
input bool   ShowHistogramOnChart = true;  // colored hist bars + values at bottom

string   g_last_bar = "";
int      g_magic = 0;
string   g_base = "";
string   g_state_file = "";
int      g_histHandle = INVALID_HANDLE;
bool     g_histOnChart = false;

int OnInit()
  {
   string m = InpModel;
   StringToUpper(m);
   if(m == "DEMO")
     {
      g_magic = InpMagicDemo;
      g_base  = InpDemoUrl;
     }
   else if(m == "ASIM")
     {
      g_magic = InpMagicAsim;
      g_base  = InpAsimUrl;
     }
   else if(m == "SOFT")
     {
      g_magic = InpMagicSoft;
      g_base  = InpSoftUrl;
     }
   else
     {
      // HARD (default / fallback)
      g_magic = InpMagicHard;
      g_base  = InpHardUrl;
      m = "HARD";
     }
   g_state_file = "onyxion_lab_bridge_" + IntegerToString(g_magic) + ".state";
   int fh = FileOpen(g_state_file, FILE_READ|FILE_TXT|FILE_COMMON);
   if(fh != INVALID_HANDLE)
     {
      g_last_bar = FileReadString(fh);
      FileClose(fh);
     }
   EventSetTimer(InpPollSeconds);
   AttachHistogramPane();
   Print("OnyxionLabBridge init model=", m, " magic=", g_magic, " url=", g_base,
         " symbol=", InpSymbol);
   return(INIT_SUCCEEDED);
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
   g_histHandle = iCustom(_Symbol, _Period, "X Trend\\OnyxionHistogram", 3, 5, 10, true, 24);
   if(g_histHandle == INVALID_HANDLE)
     {
      ResetLastError();
      g_histHandle = iCustom(_Symbol, _Period, "OnyxionHistogram", 3, 5, 10, true, 24);
     }
   if(g_histHandle == INVALID_HANDLE)
     {
      Print("WARN: OnyxionHistogram not found err=", GetLastError(),
            " — compile Indicators\\X Trend\\OnyxionHistogram.mq5 then reattach");
      return;
     }

   long cid = ChartID();
   if(HistogramAlreadyOnChart(cid))
     {
      g_histOnChart = true;
      Print("Histogram pane already on chart");
      return;
     }

   int sub = (int)ChartGetInteger(cid, CHART_WINDOWS_TOTAL);
   ResetLastError();
   if(ChartIndicatorAdd(cid, sub, g_histHandle))
     {
      g_histOnChart = true;
      Print("Histogram pane attached (colored bars + values, subwindow ", sub, ")");
     }
   else
      Print("WARN: ChartIndicatorAdd histogram err=", GetLastError(),
            " — drag OnyxionHistogram from Navigator (Indicators → X Trend) onto the chart");
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(g_histHandle != INVALID_HANDLE && !g_histOnChart)
      IndicatorRelease(g_histHandle);
  }

void OnTimer()
  {
   SyncFromLab();
  }

void OnTick() {}

//--- very small JSON string field extractor (flat keys only)
string JsonStr(const string json, const string key)
  {
   string pat = "\"" + key + "\":\"";
   int p = StringFind(json, pat);
   if(p < 0) return "";
   p += StringLen(pat);
   int e = StringFind(json, "\"", p);
   if(e < 0) return "";
   return StringSubstr(json, p, e - p);
  }

int LastKeyPos(const string json, const string key)
  {
   int last = -1, from = 0;
   while(true)
     {
      int p = StringFind(json, key, from);
      if(p < 0) break;
      last = p;
      from = p + 1;
     }
   return last;
  }

string JsonLastStr(const string json, const string key)
  {
   string pat = "\"" + key + "\":";
   int p = LastKeyPos(json, pat);
   if(p < 0) return "";
   p += StringLen(pat);
   while(p < StringLen(json) && StringGetCharacter(json, p) == ' ') p++;
   if(p >= StringLen(json) || StringGetCharacter(json, p) != '\"') return "";
   p++;
   int e = StringFind(json, "\"", p);
   return e < 0 ? "" : StringSubstr(json, p, e - p);
  }

double JsonLastNum(const string json, const string key)
  {
   string pat = "\"" + key + "\":";
   int p = LastKeyPos(json, pat);
   if(p < 0) return 0.0;
   p += StringLen(pat);
   while(p < StringLen(json) && StringGetCharacter(json, p) == ' ') p++;
   int e = p;
   while(e < StringLen(json))
     {
      ushort ch = (ushort)StringGetCharacter(json, e);
      if(ch == ',' || ch == '}' || ch == ']') break;
      e++;
     }
   string raw = StringSubstr(json, p, e - p);
   if(raw == "null") return 0.0;
   return StringToDouble(raw);
  }

bool JsonLastBool(const string json, const string key)
  {
   string pat = "\"" + key + "\":true";
   return LastKeyPos(json, pat) >= 0 &&
          LastKeyPos(json, pat) > LastKeyPos(json, "\"" + key + "\":false");
  }

bool FetchLab(string &action, string &bar_time, string &broker_entry,
              double &broker_volume, double &broker_entry_sl,
              bool &broker_close_all, int &broker_partial_count,
              double &broker_partial_volume, double &broker_partial_entry,
              double &broker_sl)
  {
   string url = g_base + "/api/lab/state";
   char data[];
   char result[];
   string headers = "Accept: application/json\r\n";
   string result_headers;
   int timeout = 15000;
   ResetLastError();
   int code = WebRequest("GET", url, headers, timeout, data, result, result_headers);
   if(code != 200)
     {
      Print("WebRequest failed code=", code, " err=", GetLastError(),
            " — add URL in Tools→Options→Expert Advisors→Allow WebRequest");
      return false;
     }
   string json = CharArrayToString(result);
   broker_entry = JsonLastStr(json, "broker_entry_action");
   broker_volume = JsonLastNum(json, "broker_entry_volume");
   broker_entry_sl = JsonLastNum(json, "broker_entry_sl");
   broker_close_all = JsonLastBool(json, "broker_close_all");
   broker_partial_count = (int)JsonLastNum(json, "broker_partial_close_count");
   broker_partial_volume = JsonLastNum(json, "broker_partial_close_volume");
   broker_partial_entry = JsonLastNum(json, "broker_partial_close_entry");
   broker_sl = JsonLastNum(json, "broker_sl");
   // walk to last "model_raw_action"
   string key = "\"model_raw_action\":\"";
   int last = -1;
   int from = 0;
   while(true)
     {
      int p = StringFind(json, key, from);
      if(p < 0) break;
      last = p;
      from = p + 1;
     }
   if(last < 0)
     {
      // fallback signal
      key = "\"signal\":\"";
      from = 0; last = -1;
      while(true)
        {
         int p = StringFind(json, key, from);
         if(p < 0) break;
         last = p;
         from = p + 1;
        }
     }
   if(last < 0)
     {
      action = "NONE";
      bar_time = "";
      return true;
     }
   int s = last + StringLen(key);
   int e = StringFind(json, "\"", s);
   action = StringSubstr(json, s, e - s);
   StringToUpper(action);
   // nearest bar_time before this action (search backwards chunk)
   int chunk_start = MathMax(0, last - 400);
   string chunk = StringSubstr(json, chunk_start, 500);
   bar_time = JsonStr(chunk, "bar_time");
   if(bar_time == "")
      bar_time = JsonStr(json, "bar_time");
   return true;
  }

int CountMine()
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != InpSymbol) continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != g_magic) continue;
      n++;
     }
   return n;
  }

int MySide() // 1 long, -1 short, 0 flat
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != InpSymbol) continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != g_magic) continue;
      long t = PositionGetInteger(POSITION_TYPE);
      return (t == POSITION_TYPE_BUY) ? 1 : -1;
     }
   return 0;
  }

bool TradeDeal(const int order_type, const double volume, const string comment, const double sl=0.0)
  {
   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action = TRADE_ACTION_DEAL;
   req.symbol = InpSymbol;
   req.volume = volume;
   req.type = (ENUM_ORDER_TYPE)order_type;
   req.deviation = InpDeviation;
   req.magic = g_magic;
   req.comment = comment;
   if(sl > 0.0) req.sl = NormalizeDouble(sl, (int)SymbolInfoInteger(InpSymbol, SYMBOL_DIGITS));
   req.type_filling = ORDER_FILLING_IOC;
   if(order_type == ORDER_TYPE_BUY)
      req.price = SymbolInfoDouble(InpSymbol, SYMBOL_ASK);
   else
      req.price = SymbolInfoDouble(InpSymbol, SYMBOL_BID);
   bool ok = OrderSend(req, res);
   Print("OrderSend type=", order_type, " vol=", volume, " ret=", res.retcode, " comment=", res.comment);
   return ok && (res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_DONE_PARTIAL);
  }

bool ClosePartialClosest(const double requested_volume, const double entry_hint)
  {
   ulong best_ticket = 0;
   double best_diff = DBL_MAX;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != InpSymbol) continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != g_magic) continue;
      double diff = entry_hint > 0.0
                    ? MathAbs(PositionGetDouble(POSITION_PRICE_OPEN) - entry_hint)
                    : 0.0;
      if(best_ticket == 0 || diff < best_diff)
        {
         best_ticket = ticket;
         best_diff = diff;
        }
     }
   if(best_ticket == 0) return true;
   if(!PositionSelectByTicket(best_ticket)) return false;
   long t = PositionGetInteger(POSITION_TYPE);
   double held = PositionGetDouble(POSITION_VOLUME);
   double vol = MathMin(held, requested_volume > 0.0 ? requested_volume : InpLot);
   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action = TRADE_ACTION_DEAL;
   req.symbol = InpSymbol;
   req.volume = vol;
   req.position = best_ticket;
   req.deviation = InpDeviation;
   req.magic = g_magic;
   req.comment = "onyxion-partial-stop";
   req.type_filling = ORDER_FILLING_IOC;
   req.type = t == POSITION_TYPE_BUY ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   req.price = t == POSITION_TYPE_BUY
               ? SymbolInfoDouble(InpSymbol, SYMBOL_BID)
               : SymbolInfoDouble(InpSymbol, SYMBOL_ASK);
   bool ok = OrderSend(req, res);
   return ok && (res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_DONE_PARTIAL);
  }

bool ModifyAllStops(const double sl)
  {
   if(sl <= 0.0) return true;
   bool all_ok = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != InpSymbol) continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != g_magic) continue;
      MqlTradeRequest req;
      MqlTradeResult res;
      ZeroMemory(req);
      ZeroMemory(res);
      req.action = TRADE_ACTION_SLTP;
      req.symbol = InpSymbol;
      req.position = ticket;
      req.sl = NormalizeDouble(sl, (int)SymbolInfoInteger(InpSymbol, SYMBOL_DIGITS));
      req.tp = PositionGetDouble(POSITION_TP);
      bool ok = OrderSend(req, res);
      if(!ok || res.retcode != TRADE_RETCODE_DONE) all_ok = false;
     }
   return all_ok;
  }

void SaveLastBar()
  {
   int fh = FileOpen(g_state_file, FILE_WRITE|FILE_TXT|FILE_COMMON);
   if(fh == INVALID_HANDLE) return;
   FileWriteString(fh, g_last_bar);
   FileClose(fh);
  }

bool CloseAll()
  {
   bool all_ok = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != InpSymbol) continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != g_magic) continue;
      long t = PositionGetInteger(POSITION_TYPE);
      double vol = PositionGetDouble(POSITION_VOLUME);
      MqlTradeRequest req;
      MqlTradeResult  res;
      ZeroMemory(req);
      ZeroMemory(res);
      req.action = TRADE_ACTION_DEAL;
      req.symbol = InpSymbol;
      req.volume = vol;
      req.position = ticket;
      req.deviation = InpDeviation;
      req.magic = g_magic;
      req.comment = "onyxion-exit";
      req.type_filling = ORDER_FILLING_IOC;
      if(t == POSITION_TYPE_BUY)
        {
         req.type = ORDER_TYPE_SELL;
         req.price = SymbolInfoDouble(InpSymbol, SYMBOL_BID);
        }
      else
        {
         req.type = ORDER_TYPE_BUY;
         req.price = SymbolInfoDouble(InpSymbol, SYMBOL_ASK);
        }
      bool ok = OrderSend(req, res);
      if(!ok || (res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_DONE_PARTIAL))
         all_ok = false;
      Print("Close #", ticket, " ret=", res.retcode);
     }
   return all_ok;
  }

void ApplyAction(const string action)
  {
   int n = CountMine();
   int side = MySide();
   Print("Apply ", action, " units=", n, " side=", side);

   if(action == "NONE" || action == "HOLD")
      return;
   if(action == "EXIT")
     {
      CloseAll();
      return;
     }

   int want = 0;
   bool add = false;
   if(action == "BUY") want = 1;
   else if(action == "SELL") want = -1;
   else if(action == "BUY_ADD") { want = 1; add = true; }
   else if(action == "SELL_ADD") { want = -1; add = true; }
   else return;

   if(side != 0 && side != want)
      CloseAll();

   n = CountMine();
   side = MySide();

   if(add)
     {
      if(n == 0)
        {
         if(want == 1) TradeDeal(ORDER_TYPE_BUY, InpLot, "onyxion-entry");
         else TradeDeal(ORDER_TYPE_SELL, InpLot, "onyxion-entry");
         return;
        }
      if(n >= InpMaxUnits) return;
      if(want == 1) TradeDeal(ORDER_TYPE_BUY, InpLot, "onyxion-add");
      else TradeDeal(ORDER_TYPE_SELL, InpLot, "onyxion-add");
      return;
     }

   if(n == 0)
     {
      if(want == 1) TradeDeal(ORDER_TYPE_BUY, InpLot, "onyxion-entry");
      else TradeDeal(ORDER_TYPE_SELL, InpLot, "onyxion-entry");
     }
  }

void SyncFromLab()
  {
   string action, bar;
   string broker_entry;
   double broker_volume = 0.0, broker_entry_sl = 0.0;
   bool broker_close_all = false;
   int broker_partial_count = 0;
   double broker_partial_volume = 0.0, broker_partial_entry = 0.0, broker_sl = 0.0;
   if(!FetchLab(action, bar, broker_entry, broker_volume, broker_entry_sl,
                broker_close_all, broker_partial_count, broker_partial_volume,
                broker_partial_entry, broker_sl))
      return;
   Print("Lab action=", action, " bar=", bar, " last=", g_last_bar);
   if(bar == "" || bar == g_last_bar)
      return;
   bool structured = broker_close_all || broker_entry != "" ||
                     broker_partial_count > 0 || broker_sl > 0.0;
   bool ok = true;
   if(structured)
     {
      if(broker_close_all)
         ok = CloseAll();
      else
        {
         double each_volume = broker_partial_count > 0
                              ? broker_partial_volume / broker_partial_count
                              : 0.0;
         for(int i = 0; i < broker_partial_count; i++)
            if(!ClosePartialClosest(each_volume, broker_partial_entry)) ok = false;
         if(ok && broker_entry == "ORDER_TYPE_BUY")
            ok = TradeDeal(ORDER_TYPE_BUY,
                           broker_volume > 0.0 ? broker_volume : InpLot,
                           "onyxion-asim-entry", broker_entry_sl);
         else if(ok && broker_entry == "ORDER_TYPE_SELL")
            ok = TradeDeal(ORDER_TYPE_SELL,
                           broker_volume > 0.0 ? broker_volume : InpLot,
                           "onyxion-asim-entry", broker_entry_sl);
         if(ok) ok = ModifyAllStops(broker_sl);
        }
     }
   else
      ApplyAction(action);
   if(!ok)
     {
      Print("Structured broker plan failed; bar remains pending: ", bar);
      return;
     }
   g_last_bar = bar;
   SaveLastBar();
  }
//+------------------------------------------------------------------+
