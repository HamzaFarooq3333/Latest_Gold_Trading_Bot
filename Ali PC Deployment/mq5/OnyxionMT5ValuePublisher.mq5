//+------------------------------------------------------------------+
//| OnyxionMT5ValuePublisher.mq5                                    |
//| Non-trading publisher for the Asim AWS live dashboard.            |
//|                                                                  |
//| Reads the exact values displayed by the MT5 custom indicators and |
//| sends them to AWS. It never opens, closes, or modifies trades.    |
//|                                                                  |
//| Attach to the same Exness XAUUSDm M15 terminal that supplies the  |
//| live account. Add the AWS URL to MT5 WebRequest allow-list.       |
//+------------------------------------------------------------------+
#property copyright "Onyxion"
#property version   "1.00"
#property strict

input string InpAwsUrl       = "https://w5ye4ffzsutba53cyfqyngi4ku0auwzw.lambda-url.us-east-1.on.aws";
input int    InpBars         = 160;
input int    InpTimerSeconds = 2;
input int    InpHistPeriod   = 3;
input int    InpHistEma      = 5;
input int    InpHistThresh   = 10;
input int    InpXtPeriod     = 6;
input double InpXtMult       = 0.8;
input int    InpXtWindow     = 160;

int g_histHandle = INVALID_HANDLE;
int g_xtHandle   = INVALID_HANDLE;

string JsonEscape(const string value)
  {
   string out = value;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   return out;
  }

string IsoTime(const datetime value)
  {
   string out = TimeToString(value, TIME_DATE|TIME_MINUTES);
   StringReplace(out, ".", "-");
   StringReplace(out, " ", "T");
   return out + ":00Z";
  }

string Number(const double value)
  {
   return DoubleToString(value, 8);
  }

string HistZone(const double value)
  {
   if(value >= InpHistThresh) return "green";
   if(value <= -InpHistThresh) return "red";
   return "amber";
  }

bool BuildPayload(string &payload)
  {
   int n = MathMax(20, MathMin(InpBars, 240));
   MqlRates rates[];
   double hist[];
   double xt[];
   ArraySetAsSeries(rates, true);
   ArraySetAsSeries(hist, true);
   ArraySetAsSeries(xt, true);

   int copied = CopyRates(_Symbol, _Period, 0, n, rates);
   if(copied < 20)
     {
      Print("MT5 value publisher: insufficient rates=", copied);
      return false;
     }
   n = copied;
   if(CopyBuffer(g_histHandle, 0, 0, n, hist) < n)
     {
      Print("MT5 value publisher: histogram buffer unavailable err=", GetLastError());
      return false;
     }
   if(CopyBuffer(g_xtHandle, 0, 0, n, xt) < n)
     {
      Print("MT5 value publisher: X-TREND buffer unavailable err=", GetLastError());
      return false;
     }

   double hao[], hac[], hah[], hal[];
   ArrayResize(hao, n);
   ArrayResize(hac, n);
   ArrayResize(hah, n);
   ArrayResize(hal, n);
   for(int c = 0; c < n; c++)
     {
      int shift = n - 1 - c;
      hac[c] = (rates[shift].open + rates[shift].high +
                rates[shift].low + rates[shift].close) / 4.0;
      hao[c] = (c == 0)
               ? (rates[shift].open + rates[shift].close) / 2.0
               : (hao[c - 1] + hac[c - 1]) / 2.0;
      hah[c] = MathMax(rates[shift].high, MathMax(hao[c], hac[c]));
      hal[c] = MathMin(rates[shift].low, MathMin(hao[c], hac[c]));
     }

   payload = "{\"values_source\":\"MT5_TERMINAL\",\"symbol\":\"" +
             JsonEscape(_Symbol) + "\",\"account\":{\"login\":" +
             IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) +
             ",\"server\":\"" +
             JsonEscape(AccountInfoString(ACCOUNT_SERVER)) +
             "\"},\"bridge\":{\"mt5_connection\":\"ONLINE\"," +
             "\"values_source\":\"MT5_TERMINAL\"," +
             "\"execution_mode\":\"MT5 VALUES\"},\"bars\":[";
   bool first = true;
   for(int c = 0; c < n; c++)
     {
      int shift = n - 1 - c;
      if(hist[shift] == EMPTY_VALUE || xt[shift] == EMPTY_VALUE)
         continue;
      if(!first) payload += ",";
      first = false;
      payload += "{\"time\":\"" + IsoTime(rates[shift].time) +
                 "\",\"open\":" + Number(hao[c]) +
                 ",\"high\":" + Number(hah[c]) +
                 ",\"low\":" + Number(hal[c]) +
                 ",\"close\":" + Number(hac[c]) +
                 ",\"candle_type\":\"HA\",\"raw_open\":" +
                 Number(rates[shift].open) + ",\"raw_high\":" +
                 Number(rates[shift].high) + ",\"raw_low\":" +
                 Number(rates[shift].low) + ",\"raw_close\":" +
                 Number(rates[shift].close) + ",\"hist\":" +
                 Number(hist[shift]) + ",\"histcolor\":\"" +
                 HistZone(hist[shift]) + "\",\"xtrend\":" +
                 Number(xt[shift]) + ",\"forming\":" +
                 (shift == 0 ? "true" : "false") + "}";
     }
   payload += "]}";
   return !first;
  }

void Publish()
  {
   string payload;
   if(!BuildPayload(payload))
      return;
   char data[];
   char result[];
   StringToCharArray(payload, data, 0, WHOLE_ARRAY, CP_UTF8);
   string headers = "Content-Type: application/json\r\nAccept: application/json\r\n";
   string response_headers;
   string url = InpAwsUrl;
   if(StringSubstr(url, StringLen(url) - 1, 1) != "/")
      url += "/";
   url += "api/broker/heartbeat";
   ResetLastError();
   int code = WebRequest("POST", url, headers, 15000, data, result, response_headers);
   if(code != 200)
     {
      Print("MT5 value publisher: WebRequest failed code=", code,
            " err=", GetLastError(),
            " — add ", InpAwsUrl,
            " in Tools -> Options -> Expert Advisors -> Allow WebRequest");
      return;
     }
   Print("MT5 value publisher: sent ", IntegerToString(StringLen(payload)),
         " bytes for ", IntegerToString(ArraySize(result)), " response bytes");
  }

int OnInit()
  {
   g_histHandle = iCustom(_Symbol, _Period, "X Trend\\OnyxionHistogram",
                          InpHistPeriod, InpHistEma, InpHistThresh, true, 24);
   if(g_histHandle == INVALID_HANDLE)
      g_histHandle = iCustom(_Symbol, _Period, "OnyxionHistogram",
                             InpHistPeriod, InpHistEma, InpHistThresh, true, 24);
   g_xtHandle = iCustom(_Symbol, _Period, "X Trend\\OnyxionXTrendProxy",
                        InpXtPeriod, InpXtMult, InpXtWindow);
   if(g_histHandle == INVALID_HANDLE || g_xtHandle == INVALID_HANDLE)
     {
      Print("MT5 value publisher: indicator handles failed hist=",
            g_histHandle, " xt=", g_xtHandle,
            " — compile the Onyxion indicators first");
      return INIT_FAILED;
     }
   EventSetTimer(MathMax(1, InpTimerSeconds));
   Print("OnyxionMT5ValuePublisher ready: ", _Symbol, " ",
         EnumToString((ENUM_TIMEFRAMES)_Period),
         " -> Asim AWS; non-trading mode");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(g_histHandle != INVALID_HANDLE) IndicatorRelease(g_histHandle);
   if(g_xtHandle != INVALID_HANDLE) IndicatorRelease(g_xtHandle);
  }

void OnTimer()
  {
   Publish();
  }

void OnTick()
  {
  }
//+------------------------------------------------------------------+
